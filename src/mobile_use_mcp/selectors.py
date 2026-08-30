"""Android UI selector resolution without model involvement.

The resolver deliberately evaluates every selector supplied by the caller
before choosing a winner. This gives action results a complete attempt ledger
and prevents a resource-id/text pair that identifies two different nodes from
silently selecting whichever fallback happens to run first.
"""

from collections.abc import Callable, Iterable
from typing import Never

from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import (
    ResolvedTarget,
    SelectorAttempt,
    Target,
    UIElement,
)


def _nth_match(
    elements: Iterable[UIElement],
    predicate: Callable[[UIElement], bool],
    index: int,
) -> tuple[int, UIElement] | None:
    matches = [
        (position, element) for position, element in enumerate(elements) if predicate(element)
    ]
    if 0 <= index < len(matches):
        return matches[index]
    return None


def _selector_error(
    selector: str,
    *,
    reason: str,
    match_count: int = 0,
) -> SelectorAttempt:
    return SelectorAttempt(
        selector=selector,
        matched=False,
        error=reason,
        match_count=match_count,
    )


def _selector_match(selector: str, *, match_count: int = 1) -> SelectorAttempt:
    return SelectorAttempt(
        selector=selector,
        matched=True,
        match_count=match_count,
    )


def _same_element(left: tuple[int, UIElement], right: tuple[int, UIElement]) -> bool:
    """Compare list identity, preserving duplicate-index semantics."""

    return left[0] == right[0]


def _raise_resolution_error(
    target: Target,
    attempts: list[SelectorAttempt],
    *,
    code: ErrorCode = ErrorCode.ELEMENT_NOT_FOUND,
    message: str | None = None,
    suggestion: str | None = None,
    data: dict[str, object] | None = None,
) -> Never:
    attempt_details = "; ".join(f"{item.selector}: {item.error or 'matched'}" for item in attempts)
    details = dict(data or {})
    details.setdefault("attempts", [attempt.model_dump(mode="json") for attempt in attempts])
    details.setdefault("matched_selector", None)
    if target.element_ref is not None:
        details.setdefault("element_ref", target.element_ref)
    raise MobileUseError(
        code,
        message or f"Unable to resolve target. Attempts: {attempt_details}",
        suggestion or "Call android_snapshot and choose a selector from the current UI elements.",
        data=details,
    )


def resolve_target(
    target: Target,
    elements: Iterable[UIElement],
    *,
    screen_width: int,
    screen_height: int,
) -> ResolvedTarget:
    """Resolve a target and cross-check all supplied semantic selectors.

    Bounds without provenance retain the historical explicit-coordinate
    behavior for embedders. Session-bound actions perform the stricter
    provenance and current-hierarchy checks in :mod:`mobile_use_mcp.session`.
    """

    # Action callers may provide a generator from a hierarchy adapter. Keep a
    # stable complete view so every selector sees the same node identities.
    elements = list(elements)
    attempts: list[SelectorAttempt] = []
    semantic_matches: list[tuple[str, tuple[int, UIElement]]] = []
    valid_bounds = False
    blocked: tuple[ErrorCode, str, str] | None = None

    if target.element_ref is not None:
        selector = "element_ref"
        match = _nth_match(
            elements,
            lambda candidate: candidate.element_ref == target.element_ref,
            0,
        )
        if match is None:
            attempts.append(
                _selector_error(selector, reason="No matching opaque element reference")
            )
        elif match[1].bounds is None:
            attempts.append(
                _selector_error(
                    selector,
                    reason="Matched element has no usable bounds",
                    match_count=1,
                )
            )
        elif not match[1].enabled:
            attempts.append(
                _selector_error(selector, reason="Matched element is disabled", match_count=1)
            )
            blocked = (
                ErrorCode.ELEMENT_DISABLED,
                "The referenced Android element is disabled.",
                "Choose an enabled element from a fresh android_snapshot.",
            )
        elif not match[1].bounds.is_within(screen_width, screen_height):
            attempts.append(
                _selector_error(selector, reason="Matched element is off-screen", match_count=1)
            )
            blocked = (
                ErrorCode.TARGET_OFF_SCREEN,
                "The referenced Android element is off-screen.",
                "Call android_snapshot and choose an element currently on screen.",
            )
        else:
            attempts.append(_selector_match(selector))
            semantic_matches.append((selector, match))

    if target.bounds is not None:
        selector = f"bounds={target.bounds.model_dump()}"
        valid_bounds = target.bounds.is_within(screen_width, screen_height)
        if valid_bounds:
            attempts.append(_selector_match(selector))
        else:
            attempts.append(
                _selector_error(
                    selector,
                    reason=f"Center is outside {screen_width}x{screen_height} screen",
                )
            )

    if target.resource_id:
        selector = f"resource_id={target.resource_id!r}[{target.resource_id_index}]"
        matching_count = sum(1 for element in elements if element.resource_id == target.resource_id)
        match = _nth_match(
            elements,
            lambda candidate: candidate.resource_id == target.resource_id,
            target.resource_id_index,
        )
        if match is None or match[1].bounds is None:
            attempts.append(
                _selector_error(
                    selector,
                    reason="No matching element with usable bounds",
                    match_count=matching_count,
                )
            )
        elif not match[1].enabled:
            attempts.append(
                _selector_error(
                    selector,
                    reason="Matched element is disabled",
                    match_count=matching_count,
                )
            )
            blocked = (
                ErrorCode.ELEMENT_DISABLED,
                "The resource-id target is disabled.",
                "Choose an enabled element from a fresh android_snapshot.",
            )
        elif not match[1].bounds.is_within(screen_width, screen_height):
            attempts.append(
                _selector_error(
                    selector,
                    reason="Matched element is off-screen",
                    match_count=matching_count,
                )
            )
            blocked = (
                ErrorCode.TARGET_OFF_SCREEN,
                "The resource-id target is off-screen.",
                "Call android_snapshot and choose an element currently on screen.",
            )
        else:
            attempts.append(_selector_match(selector, match_count=matching_count))
            semantic_matches.append((selector, match))

    if target.text:
        query = target.text.casefold()
        selector = f"text={target.text!r}[{target.text_index}]"
        matching_count = sum(
            1
            for element in elements
            if query
            in {
                (element.text or "").casefold(),
                (element.content_description or "").casefold(),
            }
        )
        match = _nth_match(
            elements,
            lambda candidate: (
                query
                in {
                    (candidate.text or "").casefold(),
                    (candidate.content_description or "").casefold(),
                }
            ),
            target.text_index,
        )
        if match is None or match[1].bounds is None:
            attempts.append(
                _selector_error(
                    selector,
                    reason="No matching text/content description with usable bounds",
                    match_count=matching_count,
                )
            )
        elif not match[1].enabled:
            attempts.append(
                _selector_error(
                    selector,
                    reason="Matched element is disabled",
                    match_count=matching_count,
                )
            )
            blocked = (
                ErrorCode.ELEMENT_DISABLED,
                "The text/content-description target is disabled.",
                "Choose an enabled element from a fresh android_snapshot.",
            )
        elif not match[1].bounds.is_within(screen_width, screen_height):
            attempts.append(
                _selector_error(
                    selector,
                    reason="Matched element is off-screen",
                    match_count=matching_count,
                )
            )
            blocked = (
                ErrorCode.TARGET_OFF_SCREEN,
                "The text/content-description target is off-screen.",
                "Call android_snapshot and choose an element currently on screen.",
            )
        else:
            attempts.append(_selector_match(selector, match_count=matching_count))
            semantic_matches.append((selector, match))

    if blocked is not None:
        code, message, suggestion = blocked
        _raise_resolution_error(
            target,
            attempts,
            code=code,
            message=message,
            suggestion=suggestion,
        )

    # An opaque capability is an identity claim, not another best-effort
    # fallback. If it is supplied but cannot be found in this hierarchy,
    # never let a semantic selector redirect the action to another node.
    if target.element_ref is not None and not any(
        attempt.selector == "element_ref" and attempt.matched for attempt in attempts
    ):
        _raise_resolution_error(
            target,
            attempts,
            code=ErrorCode.ELEMENT_REF_NOT_FOUND,
            message="The supplied opaque element reference is not present in this hierarchy.",
            suggestion="Call android_snapshot and use an element_ref from the current UI.",
        )

    # Only semantic selectors identify a node. Raw, unprovenanced bounds remain
    # an explicit coordinate request for compatibility with direct Controller
    # users; session-bound bounds are cross-checked by DeviceSession.
    if len(semantic_matches) > 1:
        first = semantic_matches[0][1]
        if any(not _same_element(first, match) for _, match in semantic_matches[1:]):
            _raise_resolution_error(
                target,
                attempts,
                code=ErrorCode.SELECTOR_CONFLICT,
                message="The supplied Android selectors identify different UI elements.",
                suggestion="Supply one selector or use selectors that identify the same element.",
                data={
                    "matched_selectors": [selector for selector, _ in semantic_matches],
                },
            )

    selected: tuple[str, tuple[int, UIElement]] | None = None
    # Preserve the documented preference order, while allowing an opaque ref
    # to win over a copied coordinate when both identify the same node.
    for selector, match in semantic_matches:
        if selector == "element_ref":
            selected = (selector, match)
            break
    if selected is None and target.bounds is not None and valid_bounds:
        selected = ("bounds", (0, UIElement(bounds=target.bounds)))
    if selected is None and semantic_matches:
        selected = semantic_matches[0]

    if selected is None:
        if target.bounds is not None and not valid_bounds:
            _raise_resolution_error(
                target,
                attempts,
                code=ErrorCode.TARGET_OFF_SCREEN,
                message="The Android target bounds are off-screen.",
                suggestion=(
                    "Call android_snapshot and choose coordinates inside the current screen."
                ),
            )
        _raise_resolution_error(
            target,
            attempts,
            message="Unable to resolve target. No selector matched a usable on-screen element.",
        )

    selector, match = selected
    selected_element = match[1]
    bounds = target.bounds if selector == "bounds" else selected_element.bounds
    if bounds is None:
        _raise_resolution_error(
            target,
            attempts,
            message="The matched Android element does not have usable bounds.",
        )

    # A proven coordinate or an element ref is a claim about node identity;
    # when semantic evidence is available, copied bounds must agree exactly.
    if (
        target.bounds is not None
        and target.bounds_provenance is not None
        and semantic_matches
        and any(
            item[1][1].bounds != target.bounds
            for item in semantic_matches
            if item[0] != "element_ref"
        )
    ) or (
        target.bounds is not None
        and target.element_ref is not None
        and semantic_matches
        and selected_element.bounds != target.bounds
    ):
        _raise_resolution_error(
            target,
            attempts,
            code=ErrorCode.SELECTOR_CONFLICT,
            message="The supplied bounds and semantic selectors identify different UI elements.",
            suggestion="Use bounds from the same snapshot as the semantic selector.",
        )

    display_selector = (
        selector
        if selector != "bounds"
        else f"bounds={target.bounds.model_dump()}"
        if target.bounds is not None
        else selector
    )
    return ResolvedTarget(
        bounds=bounds,
        selector=display_selector,
        matched_selector=display_selector,
        element=None if selector == "bounds" else selected_element,
        attempts=attempts,
    )
