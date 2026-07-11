import pytest
from pydantic import ValidationError

from mobile_use_mcp.models import Bounds, Target, UIElement


def test_bounds_center_and_screen_validation() -> None:
    bounds = Bounds(x=10, y=20, width=100, height=50)

    assert bounds.center == (60, 45)
    assert bounds.is_within(1080, 2400)
    assert not Bounds(x=1080, y=20, width=10, height=10).is_within(1080, 2400)


def test_target_requires_at_least_one_selector() -> None:
    with pytest.raises(ValidationError, match="At least one"):
        Target()


def test_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        Bounds(x=1, y=2, width=3, height=4, unexpected=True)  # type: ignore[call-arg]


def test_ui_element_semantics() -> None:
    element = UIElement(text="Continue", clickable=True)

    assert element.has_semantic_content()
    assert element.is_interactive()
