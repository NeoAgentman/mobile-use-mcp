"""Opt-in tests that require an explicitly selected Android device."""

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from mobile_use_mcp.controller import AndroidController
from mobile_use_mcp.devices import DeviceRegistry

pytestmark = pytest.mark.android


@pytest_asyncio.fixture
async def android_controller() -> AsyncIterator[AndroidController]:
    serial = os.getenv("MOBILE_USE_ANDROID_SERIAL")
    if not serial:
        pytest.skip("Set MOBILE_USE_ANDROID_SERIAL to opt in to tests that operate a real device.")

    selected = DeviceRegistry().select(serial)
    controller = AndroidController(selected.serial)
    controller.android_client.connect()
    try:
        yield controller
    finally:
        controller.disconnect()


async def test_capture_real_snapshot(android_controller: AndroidController) -> None:
    snapshot = await android_controller.snapshot(max_elements=200)

    assert snapshot.screenshot_png.startswith(b"\x89PNG")
    assert snapshot.width > 0
    assert snapshot.height > 0
    assert snapshot.serial == os.environ["MOBILE_USE_ANDROID_SERIAL"]


async def test_launch_settings_and_basic_navigation(
    android_controller: AndroidController,
) -> None:
    await android_controller.press_key("home")
    foreground = await android_controller.launch_app(
        "com.android.settings",
        retries=2,
        timeout_seconds=10,
    )
    assert foreground.package == "com.android.settings"

    snapshot = await android_controller.snapshot(interactive_only=True)
    center_x = snapshot.width // 2
    await android_controller.swipe(
        center_x,
        int(snapshot.height * 0.75),
        center_x,
        int(snapshot.height * 0.25),
        duration_ms=500,
    )
    await android_controller.press_key("back")
    await android_controller.press_key("home")
