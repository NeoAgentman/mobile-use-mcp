from dataclasses import dataclass
from io import BytesIO
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

from mobile_use_mcp.android_client import AndroidClient
from mobile_use_mcp.controller import AndroidController
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import ForegroundApp, ScreenshotCapture, ScreenSnapshot, UIElement
from mobile_use_mcp.server import android_screenshot, session
from mobile_use_mcp.session import SessionManager
from mobile_use_mcp.snapshot import SnapshotStore


def _snapshot(text: str) -> ScreenSnapshot:
    image = BytesIO()
    Image.new("RGB", (8, 12), "white").save(image, format="PNG")
    return ScreenSnapshot(
        serial="ABC",
        width=8,
        height=12,
        screenshot_png=image.getvalue(),
        elements=[UIElement(text=text)],
        foreground_app=ForegroundApp(package="com.example"),
    )


def test_snapshot_store_evicts_oldest_and_keeps_entries_immutable() -> None:
    store = SnapshotStore(max_entries=2, max_bytes=1_000_000, ttl_seconds=60)
    first = _snapshot("first")
    first_id = store.put(first).snapshot_id
    second_id = store.put(_snapshot("second")).snapshot_id
    third_id = store.put(_snapshot("third")).snapshot_id

    assert first_id is not None
    assert second_id is not None
    assert third_id is not None
    first.elements[0].text = "mutated caller copy"
    with pytest.raises(MobileUseError) as stale:
        store.get(first_id)
    assert stale.value.code == ErrorCode.SNAPSHOT_STALE
    assert store.get(second_id).elements[0].text == "second"


def test_snapshot_store_reports_ttl_expiry_separately() -> None:
    now = [0.0]
    store = SnapshotStore(max_entries=2, max_bytes=1_000_000, ttl_seconds=5, clock=lambda: now[0])
    snapshot_id = store.put(_snapshot("expiring")).snapshot_id
    assert snapshot_id is not None

    now[0] = 5.0
    with pytest.raises(MobileUseError) as expired:
        store.get(snapshot_id)
    assert expired.value.code == ErrorCode.SNAPSHOT_EXPIRED


def test_late_snapshot_commit_cannot_replace_new_revision() -> None:
    manager = SessionManager()
    context = manager.observation_context()
    manager.advance_screen_revision()

    with pytest.raises(MobileUseError) as stale:
        manager.store_snapshot(_snapshot("late"), expected_context=context)

    assert stale.value.code == ErrorCode.SNAPSHOT_STALE
    assert manager.current_snapshot_id is None


@dataclass
class _RunningApp:
    package: str = "com.example"
    activity: str = ".Main"


class _PixelsOnlyDevice:
    @property
    def info(self) -> dict[str, object]:
        return {}

    def screenshot(self) -> Image.Image:
        return Image.new("RGB", (8, 12), "white")

    def dump_hierarchy(self, compressed: bool = True) -> str:
        raise AssertionError("hierarchy must not be read for screenshots")

    def press(self, key: str) -> bool:
        return True

    def set_input_ime(self, enabled: bool = True) -> None:
        pass

    def send_keys(self, text: str) -> None:
        pass

    def clear_text(self) -> None:
        pass


class _NoForegroundADB:
    def click(self, x: int, y: int, display_id: int | None = None) -> None:
        pass

    def swipe(self, sx: int, sy: int, ex: int, ey: int, duration: float = 1.0) -> None:
        pass

    def keyevent(self, key_code: int | str) -> None:
        pass

    def app_start(self, package_name: str, activity: str | None = None) -> None:
        pass

    def app_stop(self, package_name: str) -> None:
        pass

    def open_browser(self, url: str) -> None:
        pass

    def app_current(self) -> _RunningApp:
        raise AssertionError("foreground app must not be read for screenshots")

    def shell(
        self,
        cmdargs: list[str],
        stream: bool = False,
        timeout: float | None = 600,
        encoding: str | None = "utf-8",
        rstrip: bool = True,
    ) -> str:
        return ""


@pytest.mark.asyncio
async def test_controller_screenshot_does_not_collect_optional_components() -> None:
    device = _PixelsOnlyDevice()
    controller = AndroidController(
        "ABC",
        android_client=AndroidClient("ABC", connector=lambda _: device),
        adb_connector=lambda _: _NoForegroundADB(),
    )

    capture = await controller.screenshot()

    assert (capture.width, capture.height) == (8, 12)


@pytest.mark.asyncio
async def test_combined_snapshot_reports_safe_component_warnings() -> None:
    device = _PixelsOnlyDevice()
    controller = AndroidController(
        "ABC",
        android_client=AndroidClient("ABC", connector=lambda _: device),
        adb_connector=lambda _: _NoForegroundADB(),
    )

    snapshot = await controller.snapshot()

    assert snapshot.elements == []
    assert any("HIERARCHY_UNAVAILABLE" in warning for warning in snapshot.warnings)
    assert any("FOREGROUND_APP_UNAVAILABLE" in warning for warning in snapshot.warnings)


@pytest.mark.asyncio
async def test_screenshot_tool_uses_hierarchy_free_controller_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = BytesIO()
    Image.new("RGB", (8, 12), "white").save(image, format="PNG")
    controller = Mock()
    controller.screenshot = AsyncMock(
        return_value=ScreenshotCapture(
            serial="ABC",
            width=8,
            height=12,
            screenshot_png=image.getvalue(),
        )
    )
    controller.snapshot = AsyncMock(side_effect=AssertionError("combined path used"))
    monkeypatch.setattr(session, "require_controller", lambda: controller)

    result = await android_screenshot()

    assert result.structuredContent is not None
    assert result.structuredContent["hierarchy_collected"] is False
    assert result.structuredContent["foreground_app_collected"] is False
    controller.screenshot.assert_awaited_once()
    controller.snapshot.assert_not_awaited()
