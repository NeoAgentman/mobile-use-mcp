import asyncio
from dataclasses import dataclass
from io import BytesIO
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

from mobile_use_mcp.android_client import AndroidClient
from mobile_use_mcp.controller import AndroidController
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import (
    Bounds,
    DeviceInfo,
    DeviceState,
    ForegroundApp,
    ScreenSnapshot,
    SessionLifecycle,
    SwipePercentage,
    Target,
    UIElement,
)
from mobile_use_mcp.selectors import resolve_target
from mobile_use_mcp.session import SessionManager


def _snapshot(elements: list[UIElement], *, width: int = 100, height: int = 200) -> ScreenSnapshot:
    image = BytesIO()
    Image.new("RGB", (width, height), "white").save(image, format="PNG")
    return ScreenSnapshot(
        serial="ABC",
        width=width,
        height=height,
        screenshot_png=image.getvalue(),
        elements=elements,
        foreground_app=ForegroundApp(package="com.example"),
    )


def test_selector_conflict_reports_all_attempts() -> None:
    elements = [
        UIElement(
            text="First", resource_id="id:first", bounds=Bounds(x=1, y=1, width=10, height=10)
        ),
        UIElement(
            text="Second", resource_id="id:second", bounds=Bounds(x=20, y=1, width=10, height=10)
        ),
    ]

    with pytest.raises(MobileUseError) as caught:
        resolve_target(
            Target(resource_id="id:first", text="Second"),
            elements,
            screen_width=100,
            screen_height=200,
        )

    assert caught.value.code == ErrorCode.SELECTOR_CONFLICT
    assert [attempt["matched"] for attempt in caught.value.data["attempts"]] == [True, True]
    assert caught.value.data["matched_selectors"] == [
        "resource_id='id:first'[0]",
        "text='Second'[0]",
    ]


def test_session_stamps_opaque_refs_and_rejects_old_revision() -> None:
    manager = SessionManager()
    manager._session_id = "session-test"  # pyright: ignore[reportPrivateUsage]
    manager._generation = 2  # pyright: ignore[reportPrivateUsage]
    manager._state = SessionLifecycle.READY  # pyright: ignore[reportPrivateUsage]
    manager._device = DeviceInfo(serial="ABC", state=DeviceState.DEVICE)  # pyright: ignore[reportPrivateUsage]
    snapshot = _snapshot(
        [UIElement(text="Continue", bounds=Bounds(x=1, y=2, width=10, height=10), clickable=True)]
    )

    snapshot_id = manager.store_snapshot(snapshot)
    element = snapshot.elements[0]
    assert element.element_ref is not None
    assert "Continue" not in element.element_ref
    assert element.bounds_provenance is not None
    assert element.bounds_provenance.snapshot_id == snapshot_id
    assert element.bounds_provenance.session_id == "session-test"
    assert element.bounds_provenance.generation == 2
    assert element.bounds_provenance.screen_revision == 0

    controller = Mock()
    controller.snapshot = AsyncMock(return_value=_snapshot(snapshot.elements))

    async def check_current_ref() -> None:
        _, resolved = await manager.resolve_target(
            Target(element_ref=element.element_ref),
            controller=controller,
        )
        assert resolved.matched_selector == "element_ref"

    asyncio.run(check_current_ref())
    manager.advance_screen_revision()
    with pytest.raises(MobileUseError) as caught:
        asyncio.run(
            manager.resolve_target(
                Target(element_ref=element.element_ref),
                controller=controller,
            )
        )
    assert caught.value.code == ErrorCode.ELEMENT_REF_STALE


@dataclass
class _RunningApp:
    package: str = "com.example"
    activity: str = ".Main"


class _U2:
    @property
    def info(self) -> dict[str, object]:
        return {}

    def screenshot(self) -> Image.Image:
        return Image.new("RGB", (100, 200), "white")

    def dump_hierarchy(self, compressed: bool = True) -> str:
        nodes = "".join(
            f'<node text="Item {index}" resource-id="id:{index}" class="Button" '
            'package="com.example" clickable="true" enabled="true" '
            f'bounds="[{index % 10},{index // 10}][{index % 10 + 1},{index // 10 + 1}]" />'
            for index in range(250)
        )
        return f"<hierarchy>{nodes}</hierarchy>"

    def press(self, key: str) -> bool:
        del key
        return True

    def set_input_ime(self, enabled: bool = True) -> None:
        del enabled

    def send_keys(self, text: str) -> None:
        del text

    def clear_text(self) -> None:
        pass


class _ADB:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []

    def click(self, x: int, y: int, display_id: int | None = None) -> None:
        self.clicks.append((x, y))

    def swipe(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def keyevent(self, key_code: int | str) -> None:
        del key_code

    def app_start(self, package_name: str, activity: str | None = None) -> None:
        del package_name, activity

    def app_stop(self, package_name: str) -> None:
        del package_name

    def open_browser(self, url: str) -> None:
        del url

    def app_current(self) -> _RunningApp:
        return _RunningApp()

    def shell(self, *args: object, **kwargs: object) -> str:
        del args, kwargs
        return ""


@pytest.mark.asyncio
async def test_controller_semantic_resolution_uses_complete_hierarchy() -> None:
    u2 = _U2()
    adb = _ADB()
    controller = AndroidController(
        "ABC",
        android_client=AndroidClient("ABC", connector=lambda _: u2),
        adb_connector=lambda _: adb,
    )

    result = await controller.tap(Target(resource_id="id:249"))

    assert result["selector"] == "resource_id='id:249'[0]"
    assert adb.clicks == [(9, 24)]


@pytest.mark.asyncio
async def test_controller_percentage_swipe_returns_native_pixels() -> None:
    u2 = _U2()
    adb = _ADB()
    controller = AndroidController(
        "ABC",
        android_client=AndroidClient("ABC", connector=lambda _: u2),
        adb_connector=lambda _: adb,
    )

    result = await controller.swipe_percentage(
        SwipePercentage(start_x=0, start_y=0, end_x=1, end_y=1),
        duration_ms=500,
    )

    assert result["start"] == {"x": 0, "y": 0}
    assert result["end"] == {"x": 99, "y": 199}
