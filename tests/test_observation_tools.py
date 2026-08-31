import asyncio
import threading
from io import BytesIO
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.types import ImageContent
from PIL import Image

import mobile_use_mcp.server as server_module
from mobile_use_mcp.config import RuntimeConfig
from mobile_use_mcp.errors import ErrorCode
from mobile_use_mcp.models import (
    AppInfo,
    AppInventory,
    DeviceInfo,
    DeviceState,
    ForegroundApp,
    ScreenSnapshot,
    UIElement,
)
from mobile_use_mcp.server import (
    android_connect,
    android_get_ui_elements,
    android_list_apps,
    android_list_devices,
    android_screenshot,
    android_snapshot,
    android_status,
    mcp,
    session,
)
from mobile_use_mcp.session import SessionManager


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

    def fake_connect(serial: str | None = None, **_options: object) -> DeviceInfo:
        assert serial == "ABC"
        return device

    monkeypatch.setattr(session, "connect", fake_connect)

    result = await android_connect("ABC")
    session._device = device  # pyright: ignore[reportPrivateUsage]
    status = await android_status()

    assert result["success"] is True
    assert status["connected"] is True
    session.disconnect()


async def test_status_reports_the_operation_active_when_status_was_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager(
        config=RuntimeConfig(operation_timeout_seconds=1, queue_timeout_seconds=1)
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def running_operation(_context: object) -> None:
        started.set()
        await release.wait()

    active_id = "op-active-work"
    active = asyncio.create_task(
        manager.run_operation(
            "active-work",
            running_operation,
            operation_id=active_id,
        )
    )
    await started.wait()
    monkeypatch.setattr(server_module, "session", manager)

    status_task = asyncio.create_task(server_module.android_status())
    await asyncio.sleep(0)
    assert not status_task.done()
    release.set()
    await active
    status = await status_task

    assert status.active_operation_id == active_id
    assert status.operation_id != active_id


async def test_timed_out_connect_cannot_commit_after_the_request_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    device = DeviceInfo(serial="ABC", state=DeviceState.DEVICE)
    registry = Mock()
    registry.select.return_value = device
    controller = Mock()

    def blocking_connect() -> None:
        started.set()
        release.wait()

    controller.android_client.connect.side_effect = blocking_connect
    runtime = RuntimeConfig(
        operation_timeout_seconds=0.03,
        queue_timeout_seconds=0.03,
        subprocess_timeout_seconds=0.2,
    )
    manager = SessionManager(
        registry=registry,
        controller_factory=lambda _serial: controller,
        config=runtime,
    )
    monkeypatch.setattr(server_module, "session", manager)

    connect = asyncio.create_task(server_module.android_connect("ABC"))
    try:
        assert await asyncio.to_thread(started.wait, 0.2)
        result = await asyncio.wait_for(connect, 0.2)
        assert result.structuredContent is not None
        assert result.structuredContent["success"] is False
        assert manager.device is None
    finally:
        release.set()

    await manager.run_operation("connect-cleanup-barrier", lambda _context: None)

    assert manager.device is None
    controller.disconnect.assert_called_once()


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


async def test_snapshot_rejects_a_serialized_response_over_the_payload_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = BytesIO()
    Image.new("RGB", (16, 16), "white").save(image, format="PNG")
    device = DeviceInfo(serial="ABC", state=DeviceState.DEVICE)
    registry = Mock()
    registry.select.return_value = device
    controller = Mock()
    controller.snapshot = AsyncMock(
        return_value=ScreenSnapshot(
            serial="ABC",
            width=16,
            height=16,
            screenshot_png=image.getvalue(),
            elements=[UIElement(text="x" * 2_000), UIElement(text="y" * 2_000)],
            foreground_app=ForegroundApp(),
        )
    )
    manager = SessionManager(
        registry=registry,
        controller_factory=lambda _serial: controller,
        config=RuntimeConfig(payload_max_bytes=4_096),
    )
    manager.connect("ABC")
    monkeypatch.setattr(server_module, "session", manager)

    result = await server_module.android_snapshot(detail_level="full", image_format="png")

    assert result.isError is True
    assert result.structuredContent is not None
    assert result.structuredContent["error_code"] == ErrorCode.SNAPSHOT_TOO_LARGE.value
    assert result.structuredContent["details"]["max_bytes"] == 4_096
    assert not any(isinstance(content, ImageContent) for content in result.content)


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


async def test_list_apps_returns_localized_inventory_with_paging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = Mock()
    controller.list_app_inventory = AsyncMock(
        return_value=AppInventory(
            apps=[
                AppInfo(
                    package="com.example.demo",
                    launcher_label="示例应用",
                    launchable_activity=".MainActivity",
                    is_system=False,
                    is_third_party=True,
                    enabled=True,
                ),
                AppInfo(
                    package="com.example.reader",
                    launcher_label="阅读器",
                    launchable_activity=".ReaderActivity",
                    is_system=False,
                    is_third_party=True,
                    enabled=True,
                ),
            ],
            metadata_status="available",
        )
    )
    monkeypatch.setattr(session, "require_controller", lambda: controller)

    result = await android_list_apps(query="示例", offset=0, limit=1)

    assert result["success"] is True
    assert result["total"] == 1
    assert result["count"] == 1
    assert result["has_more"] is False
    assert result["apps"][0]["launcher_label"] == "示例应用"  # type: ignore[index]
    controller.list_app_inventory.assert_awaited_once_with(query="示例", include_system=True)


async def test_list_apps_no_match_is_typed_business_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = Mock()
    controller.list_app_inventory = AsyncMock(return_value=AppInventory(apps=[]))
    monkeypatch.setattr(session, "require_controller", lambda: controller)

    result = await android_list_apps(query="missing-app")

    assert result["success"] is False
    assert result["error_code"] == "APP_NOT_FOUND"
