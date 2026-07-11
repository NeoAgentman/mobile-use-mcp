"""Conversion of Android UIAutomator hierarchy data into compact MCP output."""

import re
import xml.etree.ElementTree as ET

from mobile_use_mcp.models import Bounds, UIElement

_BOUNDS_PATTERN = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")


def parse_bounds(value: str | None) -> Bounds | None:
    if not value:
        return None
    match = _BOUNDS_PATTERN.fullmatch(value)
    if match is None:
        return None
    left, top, right, bottom = (int(group) for group in match.groups())
    if right <= left or bottom <= top:
        return None
    return Bounds(x=left, y=top, width=right - left, height=bottom - top)


def _as_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.casefold() == "true"


def _clean(value: str | None, max_length: int) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[:max_length]


def parse_hierarchy(
    hierarchy_xml: str,
    *,
    interactive_only: bool = False,
    max_elements: int = 200,
    max_text_length: int = 500,
) -> list[UIElement]:
    """Flatten and compact a UIAutomator XML hierarchy."""

    if max_elements < 1:
        return []
    try:
        root = ET.fromstring(hierarchy_xml)
    except ET.ParseError as error:
        raise ValueError("UIAutomator returned invalid hierarchy XML") from error

    elements: list[UIElement] = []
    for node in root.iter("node"):
        attributes = node.attrib
        element = UIElement(
            text=_clean(attributes.get("text"), max_text_length),
            content_description=_clean(attributes.get("content-desc"), max_text_length),
            resource_id=_clean(attributes.get("resource-id"), 512),
            class_name=_clean(attributes.get("class"), 512),
            package=_clean(attributes.get("package"), 512),
            bounds=parse_bounds(attributes.get("bounds")),
            clickable=_as_bool(attributes.get("clickable")),
            enabled=_as_bool(attributes.get("enabled"), default=True),
            focusable=_as_bool(attributes.get("focusable")),
            focused=_as_bool(attributes.get("focused")),
            scrollable=_as_bool(attributes.get("scrollable")),
            selected=_as_bool(attributes.get("selected")),
            checked=(
                _as_bool(attributes.get("checked"))
                if attributes.get("checkable") == "true"
                else None
            ),
        )
        if element.bounds is None:
            continue
        if interactive_only and not element.is_interactive():
            continue
        if not element.has_semantic_content() and not element.is_interactive():
            continue
        elements.append(element)
        if len(elements) >= max_elements:
            break
    return elements
