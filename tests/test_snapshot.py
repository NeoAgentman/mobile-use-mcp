import pytest

from mobile_use_mcp.snapshot import parse_bounds, parse_hierarchy

HIERARCHY = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout"
        package="com.example" content-desc="" clickable="false" enabled="true"
        focusable="false" focused="false" scrollable="false" selected="false"
        checkable="false" checked="false" bounds="[0,0][1080,2400]">
    <node index="0" text="Continue" resource-id="com.example:id/continue"
          class="android.widget.Button" package="com.example" content-desc="Next step"
          clickable="true" enabled="true" focusable="true" focused="false"
          scrollable="false" selected="false" checkable="false" checked="false"
          bounds="[20,100][300,180]" />
    <node index="1" text="Informational" resource-id="" class="android.widget.TextView"
          package="com.example" content-desc="" clickable="false" enabled="true"
          focusable="false" focused="false" scrollable="false" selected="false"
          checkable="false" checked="false" bounds="[20,200][500,260]" />
  </node>
</hierarchy>
"""


def test_parse_bounds() -> None:
    bounds = parse_bounds("[20,100][300,180]")

    assert bounds is not None
    assert bounds.model_dump() == {"x": 20, "y": 100, "width": 280, "height": 80}
    assert parse_bounds("invalid") is None
    assert parse_bounds("[20,100][20,180]") is None


def test_parse_hierarchy_compacts_elements() -> None:
    elements = parse_hierarchy(HIERARCHY)

    assert len(elements) == 2
    assert elements[0].text == "Continue"
    assert elements[0].content_description == "Next step"
    assert elements[0].clickable
    assert elements[1].text == "Informational"


def test_interactive_only_filters_static_text() -> None:
    elements = parse_hierarchy(HIERARCHY, interactive_only=True)

    assert len(elements) == 1
    assert elements[0].resource_id == "com.example:id/continue"


def test_parse_hierarchy_respects_limit_and_text_length() -> None:
    elements = parse_hierarchy(HIERARCHY, max_elements=1, max_text_length=4)

    assert len(elements) == 1
    assert elements[0].text == "Cont"


def test_invalid_xml_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid hierarchy XML"):
        parse_hierarchy("<broken")
