from unittest.mock import AsyncMock, Mock

import pytest
from mcp.types import ImageContent

from mobile_use_mcp.models import (
    DeviceInfo,
    DeviceState,
    ForegroundApp,
    ScreenSnapshot,
    UIElement,
)
from mobile_use_mcp.server import (
    android_connect,
    android_list_devices,
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
    controller = Mock()
    controller.snapshot = AsyncMock(
        return_value=ScreenSnapshot(
            serial="ABC",
            width=1080,
            height=2400,
            screenshot_png=b"png-data",
            elements=[UIElement(text="Continue", clickable=True)],
            foreground_app=ForegroundApp(package="com.example", activity=".Main"),
        )
    )
    monkeypatch.setattr(session, "require_controller", lambda: controller)

    result = await android_snapshot()

    assert result.structuredContent is not None
    assert result.structuredContent["element_count"] == 1
    assert any(isinstance(content, ImageContent) for content in result.content)


async def test_snapshot_hides_internal_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = Mock()
    controller.snapshot = AsyncMock(side_effect=RuntimeError("secret internal detail"))
    monkeypatch.setattr(session, "require_controller", lambda: controller)

    result = await android_snapshot()

    assert result.isError is True
    assert "secret internal detail" not in str(result)
    assert result.structuredContent is not None
    assert result.structuredContent["error_code"] == "OPERATION_FAILED"
