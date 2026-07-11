from io import BytesIO
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.types import ImageContent
from PIL import Image

from mobile_use_mcp.models import (
    DeviceInfo,
    DeviceState,
    ForegroundApp,
    ScreenSnapshot,
    UIElement,
)
from mobile_use_mcp.server import (
    android_connect,
    android_get_ui_elements,
    android_list_devices,
    android_screenshot,
    android_snapshot,
    android_status,
    mcp,
    session,
)


async def test_expected_observation_tools_are_registered() -> None:
    names = {tool.name for tool in await mcp.list_tools()}

    assert {
        "android_list_devices",
        "android_connect",
        "android_status",
        "android_disconnect",
        "android_snapshot",
        "android_screenshot",
        "android_get_ui_elements",
        "android_get_foreground_app",
        "android_list_apps",
    } <= names


async def test_list_devices_returns_paginated_structured_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session.registry,
        "list_devices",
        lambda: [DeviceInfo(serial="ABC", state=DeviceState.DEVICE)],
    )

    result = await android_list_devices()

    assert result["success"] is True
    assert result["total"] == 1
    assert result["devices"][0]["serial"] == "ABC"  # type: ignore[index]


async def test_connect_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    device = DeviceInfo(serial="ABC", state=DeviceState.DEVICE)

    def fake_connect(serial: str | None = None) -> DeviceInfo:
        assert serial == "ABC"
        return device

    monkeypatch.setattr(session, "connect", fake_connect)

    result = await android_connect("ABC")
    session._device = device  # pyright: ignore[reportPrivateUsage]
    status = await android_status()

    assert result["success"] is True
    assert status["connected"] is True
    session.disconnect()


async def test_snapshot_returns_image_and_structured_ui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = BytesIO()
    Image.new("RGB", (1080, 2400)).save(image, format="PNG")
    controller = Mock()
    controller.snapshot = AsyncMock(
        return_value=ScreenSnapshot(
            serial="ABC",
            width=1080,
            height=2400,
            screenshot_png=image.getvalue(),
            elements=[UIElement(text="Continue", clickable=True)],
            foreground_app=ForegroundApp(package="com.example", activity=".Main"),
        )
    )
    monkeypatch.setattr(session, "require_controller", lambda: controller)

    result = await android_snapshot()

    assert result.structuredContent is not None
    assert result.structuredContent["element_count"] == 1
    assert result.structuredContent["total_elements"] == 1
    assert result.structuredContent["truncated"] is False
    assert str(result.structuredContent["snapshot_id"]).startswith("s-")
    assert result.structuredContent["image_format"] == "jpeg"
    assert result.structuredContent["image_quality"] == 60
    assert result.structuredContent["image_lossless"] is False
    assert result.structuredContent["image_width"] == 1080
    assert result.structuredContent["image_height"] == 2400
    assert result.structuredContent["lossless_fallback"] == {
        "tool": "android_snapshot",
        "arguments": {"image_format": "png"},
    }
    assert 'image_format="png"' in result.structuredContent["quality_notice"]
    assert any(isinstance(content, ImageContent) for content in result.content)


async def test_snapshot_supports_explicit_lossless_png(monkeypatch: pytest.MonkeyPatch) -> None:
    image = BytesIO()
    Image.new("RGB", (1080, 2400)).save(image, format="PNG")
    controller = Mock()
    controller.snapshot = AsyncMock(
        return_value=ScreenSnapshot(
            serial="ABC",
            width=1080,
            height=2400,
            screenshot_png=image.getvalue(),
            elements=[],
            foreground_app=ForegroundApp(),
        )
    )
    monkeypatch.setattr(session, "require_controller", lambda: controller)

    result = await android_snapshot(image_format="png")

    assert result.structuredContent is not None
    assert result.structuredContent["width"] == 1080
    assert result.structuredContent["image_width"] == 1080
    assert result.structuredContent["image_height"] == 2400
    assert result.structuredContent["image_lossless"] is True
    assert result.structuredContent["image_quality"] is None
    assert result.structuredContent["lossless_fallback"] is None
    image_content = next(item for item in result.content if isinstance(item, ImageContent))
    assert image_content.mimeType == "image/png"


async def test_screenshot_default_mentions_lossless_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = BytesIO()
    Image.new("RGB", (1080, 2400)).save(image, format="PNG")
    controller = Mock()
    controller.snapshot = AsyncMock(
        return_value=ScreenSnapshot(
            serial="ABC",
            width=1080,
            height=2400,
            screenshot_png=image.getvalue(),
            elements=[],
            foreground_app=ForegroundApp(),
        )
    )
    monkeypatch.setattr(session, "require_controller", lambda: controller)

    result = await android_screenshot()

    assert result.structuredContent is not None
    assert result.structuredContent["image_format"] == "jpeg"
    assert result.structuredContent["image_quality"] == 60
    assert result.structuredContent["lossless_fallback"] == {
        "tool": "android_screenshot",
        "arguments": {"image_format": "png"},
    }


async def test_snapshot_marks_truncation_and_supports_full_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = BytesIO()
    Image.new("RGB", (1080, 2400)).save(image, format="PNG")
    elements = [UIElement(text=f"Item {index}") for index in range(3)]
    controller = Mock()
    controller.snapshot = AsyncMock(
        return_value=ScreenSnapshot(
            serial="ABC",
            width=1080,
            height=2400,
            screenshot_png=image.getvalue(),
            elements=elements,
            foreground_app=ForegroundApp(),
        )
    )
    monkeypatch.setattr(session, "require_controller", lambda: controller)

    compact = await android_snapshot(max_elements=2)
    full = await android_snapshot(detail_level="full", max_elements=1)

    assert compact.structuredContent is not None
    assert compact.structuredContent["returned_elements"] == 2
    assert compact.structuredContent["total_elements"] == 3
    assert compact.structuredContent["truncated"] is True
    assert compact.structuredContent["next_offset"] == 2
    assert compact.structuredContent["full_fallback"]["arguments"] == {  # type: ignore[index]
        "detail_level": "full"
    }
    assert full.structuredContent is not None
    assert full.structuredContent["returned_elements"] == 3
    assert full.structuredContent["truncated"] is False


async def test_snapshot_hides_internal_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = Mock()
    controller.snapshot = AsyncMock(side_effect=RuntimeError("secret internal detail"))
    monkeypatch.setattr(session, "require_controller", lambda: controller)

    result = await android_snapshot()

    assert result.isError is True
    assert "secret internal detail" not in str(result)
    assert result.structuredContent is not None
    assert result.structuredContent["error_code"] == "OPERATION_FAILED"


async def test_ui_elements_pages_and_queries_one_cached_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = Mock()
    controller.snapshot = AsyncMock(
        return_value=ScreenSnapshot(
            serial="ABC",
            width=1080,
            height=2400,
            screenshot_png=b"png",
            elements=[
                UIElement(text="校招信息", package="com.example", clickable=True),
                UIElement(
                    content_description="校招搜索按钮",
                    package="com.example",
                    clickable=True,
                ),
                UIElement(text="其他内容", package="com.other"),
            ],
            foreground_app=ForegroundApp(package="com.example"),
        )
    )
    monkeypatch.setattr(session, "require_controller", lambda: controller)

    first = await android_get_ui_elements(query="校招", package="com.example", limit=1)
    second = await android_get_ui_elements(
        snapshot_id=str(first["snapshot_id"]),
        query="校招",
        package="com.example",
        offset=1,
        limit=1,
    )

    assert first["snapshot_total_elements"] == 3
    assert first["total_matches"] == 2
    assert first["count"] == 1
    assert first["has_more"] is True
    assert first["next_offset"] == 1
    assert second["count"] == 1
    assert second["has_more"] is False
    assert second["next_offset"] is None
    assert second["snapshot_id"] == first["snapshot_id"]
    controller.snapshot.assert_awaited_once()


async def test_ui_elements_rejects_expired_snapshot_id() -> None:
    result = await android_get_ui_elements(snapshot_id="s-expired")

    assert result["success"] is False
    assert result["error_code"] == "SNAPSHOT_NOT_FOUND"
