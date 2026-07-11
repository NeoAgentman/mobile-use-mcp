"""Opt-in tests that require an explicitly selected Android device."""

import asyncio
import os
import shutil
import sys
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ImageContent

from mobile_use_mcp.controller import AndroidController
from mobile_use_mcp.devices import DeviceRegistry
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import Target

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
    assert foreground.foreground_app.package == "com.android.settings"

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


async def test_unicode_text_input_and_clear(android_controller: AndroidController) -> None:
    test_text = "MCP 你好 & 123"
    await android_controller.launch_app(
        "com.android.settings",
        retries=2,
        timeout_seconds=10,
    )
    for _ in range(3):
        await android_controller.swipe(540, 400, 540, 2_000, duration_ms=400)

    target = Target(resource_id="android:id/search_src_text")
    snapshot = await android_controller.snapshot(max_elements=300)
    if not any(element.resource_id == target.resource_id for element in snapshot.elements):
        pytest.skip("This Settings build does not expose android:id/search_src_text")

    try:
        assert await android_controller.clear_text(target=target) == "uiautomator2"
        assert await android_controller.type_text(test_text, target=target) == "uiautomator2"
        updated = await android_controller.snapshot(max_elements=300)
        search_field = next(
            element for element in updated.elements if element.resource_id == target.resource_id
        )
        assert search_field.text == test_text
    finally:
        await android_controller.clear_text(target=target)
        await android_controller.press_key("home")


async def test_real_screen_recording(android_controller: AndroidController) -> None:
    try:
        await android_controller.start_recording(max_duration_seconds=10, bit_rate=2_000_000)
    except MobileUseError as error:
        if error.code == ErrorCode.UNSUPPORTED:
            pytest.skip("This Android build does not provide screenrecord")
        raise
    await asyncio.sleep(2)
    result = await android_controller.stop_recording()

    recording_path = Path(str(result["recording_path"]))
    try:
        assert recording_path.exists()
        assert recording_path.stat().st_size > 0
        assert result["segment_count"] == 1
    finally:
        shutil.rmtree(recording_path.parent, ignore_errors=True)


async def test_stdio_mcp_on_real_device() -> None:
    serial = os.environ["MOBILE_USE_ANDROID_SERIAL"]
    project_root = Path(__file__).resolve().parents[2]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mobile_use_mcp"],
        cwd=project_root,
    )

    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        connected = await client.call_tool(
            "android_connect",
            {"serial": serial},
            read_timeout_seconds=timedelta(seconds=30),
        )
        try:
            snapshot = await client.call_tool(
                "android_snapshot",
                {
                    "max_elements": 200,
                },
                read_timeout_seconds=timedelta(seconds=30),
            )
            assert snapshot.structuredContent is not None
            snapshot_id = str(snapshot.structuredContent["snapshot_id"])
            first_page = await client.call_tool(
                "android_get_ui_elements",
                {
                    "snapshot_id": snapshot_id,
                    "offset": 0,
                    "limit": 10,
                    "package": "com.android.settings",
                },
                read_timeout_seconds=timedelta(seconds=30),
            )
            launched = await client.call_tool(
                "android_launch_app",
                {"package": "com.android.settings"},
                read_timeout_seconds=timedelta(seconds=60),
            )
        finally:
            await client.call_tool(
                "android_press_key",
                {"key": "home"},
                read_timeout_seconds=timedelta(seconds=30),
            )
            await client.call_tool(
                "android_disconnect",
                read_timeout_seconds=timedelta(seconds=30),
            )

    assert connected.structuredContent is not None
    assert connected.structuredContent["success"] is True
    assert snapshot.structuredContent is not None
    assert snapshot.structuredContent["serial"] == serial
    assert snapshot.structuredContent["image_format"] == "jpeg"
    assert snapshot.structuredContent["image_quality"] == 60
    assert snapshot.structuredContent["image_lossless"] is False
    assert snapshot.structuredContent["image_width"] == snapshot.structuredContent["width"]
    assert snapshot.structuredContent["image_height"] == snapshot.structuredContent["height"]
    assert snapshot.structuredContent["lossless_fallback"] == {
        "tool": "android_snapshot",
        "arguments": {"image_format": "png"},
    }
    assert snapshot.structuredContent["element_count"] > 0
    assert (
        snapshot.structuredContent["total_elements"] >= snapshot.structuredContent["element_count"]
    )
    image = next(content for content in snapshot.content if isinstance(content, ImageContent))
    assert image.mimeType == "image/jpeg"
    assert first_page.structuredContent is not None
    assert first_page.structuredContent["snapshot_id"] == snapshot_id
    assert first_page.structuredContent["count"] <= 10
    assert first_page.structuredContent["package"] == "com.android.settings"
    assert launched.structuredContent is not None
    assert launched.structuredContent["data"]["foreground_app"]["package"] == (
        "com.android.settings"
    )
