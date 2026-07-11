"""Android UI selector resolution without model involvement."""

from collections.abc import Callable, Iterable

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
) -> UIElement | None:
    matches = (element for element in elements if predicate(element))
    return next((element for current, element in enumerate(matches) if current == index), None)


def resolve_target(
    target: Target,
    elements: list[UIElement],
    *,
    screen_width: int,
    screen_height: int,
) -> ResolvedTarget:
    """Resolve a target using bounds, resource ID, then visible text."""

    attempts: list[SelectorAttempt] = []

    if target.bounds is not None:
        selector = f"bounds={target.bounds.model_dump()}"
        if target.bounds.is_within(screen_width, screen_height):
            attempts.append(SelectorAttempt(selector=selector, matched=True))
            return ResolvedTarget(
                bounds=target.bounds,
                selector=selector,
                attempts=attempts,
            )
        attempts.append(
            SelectorAttempt(
                selector=selector,
                matched=False,
                error=f"Center is outside {screen_width}x{screen_height} screen",
            )
        )

    if target.resource_id:
        selector = f"resource_id={target.resource_id!r}[{target.resource_id_index}]"
        element = _nth_match(
            elements,
            lambda candidate: candidate.resource_id == target.resource_id,
            target.resource_id_index,
        )
        if element and element.bounds:
            attempts.append(SelectorAttempt(selector=selector, matched=True))
            return ResolvedTarget(
                bounds=element.bounds,
                selector=selector,
                element=element,
                attempts=attempts,
            )
        attempts.append(
            SelectorAttempt(
                selector=selector,
                matched=False,
                error="No matching element with usable bounds",
            )
        )

    if target.text:
        query = target.text.casefold()
        selector = f"text={target.text!r}[{target.text_index}]"
        element = _nth_match(
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
        if element and element.bounds:
            attempts.append(SelectorAttempt(selector=selector, matched=True))
            return ResolvedTarget(
                bounds=element.bounds,
                selector=selector,
                element=element,
                attempts=attempts,
            )
        attempts.append(
            SelectorAttempt(
                selector=selector,
                matched=False,
                error="No matching text/content description with usable bounds",
            )
        )

    details = "; ".join(f"{item.selector}: {item.error}" for item in attempts)
    raise MobileUseError(
        ErrorCode.ELEMENT_NOT_FOUND,
        f"Unable to resolve target. Attempts: {details}",
        "Call android_snapshot and choose a selector from the current UI elements.",
    )
