import pytest

from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import Bounds, Target, UIElement
from mobile_use_mcp.selectors import resolve_target

ELEMENTS = [
    UIElement(
        text="Continue",
        resource_id="com.example:id/action",
        bounds=Bounds(x=10, y=20, width=100, height=40),
        clickable=True,
    ),
    UIElement(
        content_description="Continue",
        resource_id="com.example:id/action",
        bounds=Bounds(x=10, y=80, width=100, height=40),
        clickable=True,
    ),
]


def test_bounds_are_preferred_when_valid() -> None:
    result = resolve_target(
        Target(
            bounds=Bounds(x=200, y=300, width=50, height=50),
            resource_id="com.example:id/action",
        ),
        ELEMENTS,
        screen_width=1080,
        screen_height=2400,
    )

    assert result.bounds.center == (225, 325)
    assert result.selector.startswith("bounds=")


def test_invalid_bounds_fall_back_to_resource_id() -> None:
    result = resolve_target(
        Target(
            bounds=Bounds(x=1200, y=300, width=50, height=50),
            resource_id="com.example:id/action",
        ),
        ELEMENTS,
        screen_width=1080,
        screen_height=2400,
    )

    assert result.bounds.center == (60, 40)
    assert result.selector == "resource_id='com.example:id/action'[0]"
    assert len(result.attempts) == 2


def test_resource_id_index_selects_nth_match() -> None:
    result = resolve_target(
        Target(resource_id="com.example:id/action", resource_id_index=1),
        ELEMENTS,
        screen_width=1080,
        screen_height=2400,
    )

    assert result.bounds.center == (60, 100)


def test_text_matches_text_or_content_description_case_insensitively() -> None:
    result = resolve_target(
        Target(text="continue", text_index=1),
        ELEMENTS,
        screen_width=1080,
        screen_height=2400,
    )

    assert result.bounds.center == (60, 100)


def test_missing_target_returns_actionable_error() -> None:
    with pytest.raises(MobileUseError) as caught:
        resolve_target(
            Target(text="Missing"),
            ELEMENTS,
            screen_width=1080,
            screen_height=2400,
        )

    assert caught.value.code == ErrorCode.ELEMENT_NOT_FOUND
    assert caught.value.suggestion == (
        "Call android_snapshot and choose a selector from the current UI elements."
    )
