from unittest.mock import AsyncMock, Mock

import pytest

from mobile_use_mcp.models import Bounds, ForegroundApp, Target
from mobile_use_mcp.server import (
    android_launch_app,
    android_open_url,
    android_press_key,
    android_swipe,
    android_tap,
    android_type_text,
    mcp,
    session,
)


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch) -> Mock:
    fake = Mock()
    fake.tap = AsyncMock(
        return_value={
            "selector": "text='Continue'[0]",
            "x": 100,
            "y": 200,
            "attempts": [],
        }
    )
    fake.swipe = AsyncMock()
    fake.type_text = AsyncMock(return_value="uiautomator2")
    fake.press_key = AsyncMock()
    fake.launch_app = AsyncMock(return_value=ForegroundApp(package="com.example", activity=".Main"))
    fake.open_url = AsyncMock()
    monkeypatch.setattr(session, "require_controller", lambda: fake)
    return fake


async def test_expected_action_tools_are_registered() -> None:
    names = {tool.name for tool in await mcp.list_tools()}
    assert {
        "android_tap",
        "android_long_press",
        "android_swipe",
        "android_type_text",
        "android_clear_text",
        "android_press_key",
        "android_launch_app",
        "android_terminate_app",
        "android_open_url",
        "android_wait",
    } <= names


async def test_tap_returns_selector_details(controller: Mock) -> None:
    result = await android_tap(Target(bounds=Bounds(x=10, y=20, width=30, height=40)))

    assert result["success"] is True
    assert result["data"]["selector"] == "text='Continue'[0]"  # type: ignore[index]
    controller.tap.assert_awaited_once()


async def test_swipe_calls_controller(controller: Mock) -> None:
    result = await android_swipe(10, 20, 30, 40, 500)

    assert result["success"] is True
    controller.swipe.assert_awaited_once_with(10, 20, 30, 40, 500)


async def test_type_text_does_not_echo_sensitive_content(controller: Mock) -> None:
    secret = "123456"
    result = await android_type_text(secret)

    assert result["success"] is True
    assert secret not in str(result)
    assert result["data"]["character_count"] == 6  # type: ignore[index]
    controller.type_text.assert_awaited_once_with(secret, None)


async def test_key_app_and_url_tools(controller: Mock) -> None:
    key_result = await android_press_key("home")
    app_result = await android_launch_app("com.example")
    url_result = await android_open_url("https://example.com")

    assert key_result["success"] is True
    assert app_result["data"]["foreground_app"]["package"] == "com.example"  # type: ignore[index]
    assert url_result["success"] is True
    controller.press_key.assert_awaited_once_with("home")
    controller.open_url.assert_awaited_once_with("https://example.com")
