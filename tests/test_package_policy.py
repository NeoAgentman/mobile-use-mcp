from unittest.mock import AsyncMock, Mock

import pytest

from mobile_use_mcp import server
from mobile_use_mcp.models import (
    Bounds,
    DeviceInfo,
    DeviceState,
    ForegroundApp,
    PackagePolicy,
    ScreenSnapshot,
    Target,
    UIElement,
)
from mobile_use_mcp.session import SessionManager


def _make_connected_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: PackagePolicy,
    foreground: ForegroundApp | None = None,
    element_package: str = "com.example",
) -> tuple[SessionManager, Mock]:
    registry = Mock()
    registry.select.return_value = DeviceInfo(serial="ABC", state=DeviceState.DEVICE)
    controller = Mock()
    controller.android_client.connect = Mock()
    controller.get_foreground_app = AsyncMock(
        return_value=foreground or ForegroundApp(package="com.example", activity=".Main")
    )
    controller.press_key = AsyncMock()
    controller.open_url = AsyncMock()
    controller.swipe = AsyncMock()
    controller.type_text = AsyncMock(return_value="uiautomator2")
    controller.clear_text = AsyncMock(return_value="uiautomator2")
    controller.long_press_resolved = AsyncMock(return_value={"x": 5, "y": 5})
    controller.launch_app = AsyncMock(return_value=ForegroundApp(package="com.example"))
    controller.terminate_app = AsyncMock(
        return_value={
            "requested_package": "com.example",
            "targeted_package": "com.example",
            "command_status": "sent",
            "effect_status": "unverified",
            "effect_verified": False,
            "verification_available": False,
            "requires_observation": True,
        }
    )
    controller.tap_resolved = AsyncMock(
        return_value={"x": 5, "y": 5, "selector": "resource_id='button'[0]"}
    )
    controller.snapshot = AsyncMock(
        return_value=ScreenSnapshot(
            serial="ABC",
            width=100,
            height=100,
            screenshot_png=b"png",
            elements=[
                UIElement(
                    resource_id="button",
                    package=element_package,
                    bounds=Bounds(x=1, y=1, width=10, height=10),
                    clickable=True,
                )
            ],
            foreground_app=foreground or ForegroundApp(package="com.example", activity=".Main"),
        )
    )
    connected = SessionManager(registry=registry, controller_factory=lambda _: controller)
    connected.connect("ABC", policy=policy)
    monkeypatch.setattr(server, "session", connected)
    return connected, controller


@pytest.mark.asyncio
async def test_denied_current_package_runs_no_key_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session, controller = _make_connected_session(
        monkeypatch,
        policy=PackagePolicy(allowed_packages=["com.example"]),
        foreground=ForegroundApp(package="com.other", activity=".Main"),
    )

    result = await server.android_press_key("back")

    assert result["success"] is False
    assert result["error_code"] == "PACKAGE_POLICY_DENIED"
    assert result["data"]["reason"] == "current_package_denied"
    controller.press_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_denied_target_package_runs_no_tap_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session, controller = _make_connected_session(
        monkeypatch,
        policy=PackagePolicy(allowed_packages=["com.example"]),
        element_package="com.other",
    )

    result = await server.android_tap(Target(resource_id="button"))

    assert result["success"] is False
    assert result["error_code"] == "PACKAGE_POLICY_DENIED"
    assert result["data"]["reason"] == "target_package_denied"
    controller.tap_resolved.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_target_package_fails_closed_before_tap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session, controller = _make_connected_session(
        monkeypatch,
        policy=PackagePolicy(allowed_packages=["com.example"]),
        element_package="",
    )

    result = await server.android_tap(Target(resource_id="button"))

    assert result["success"] is False
    assert result["error_code"] == "PACKAGE_POLICY_DENIED"
    assert result["data"]["reason"] == "target_package_unavailable"
    controller.tap_resolved.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreground_read_failure_fails_closed_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected, controller = _make_connected_session(
        monkeypatch,
        policy=PackagePolicy(allowed_packages=["com.example"]),
    )
    del connected
    controller.get_foreground_app.side_effect = RuntimeError("private adapter detail")

    result = await server.android_press_key("back")

    assert result["success"] is False
    assert result["error_code"] == "PACKAGE_POLICY_FOREGROUND_UNAVAILABLE"
    assert "private adapter detail" not in str(result)
    controller.press_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_policy_checker_fails_closed_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected, controller = _make_connected_session(
        monkeypatch,
        policy=PackagePolicy(allowed_packages=["com.example"]),
    )
    monkeypatch.setattr(connected, "check_package_policy", None)

    result = await server.android_press_key("back")

    assert result["success"] is False
    assert result["error_code"] == "PACKAGE_POLICY_FOREGROUND_UNAVAILABLE"
    assert result["data"]["reason"] == "policy_checker_unavailable"
    controller.press_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_targeted_input_without_resolver_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session, controller = _make_connected_session(
        monkeypatch,
        policy=PackagePolicy(allowed_packages=["com.example"]),
    )
    controller.tap_resolved = None
    controller.verify_focused_element = None

    result = await server.android_type_text("secret", Target(text="button"))

    assert result["success"] is False
    assert result["error_code"] == "PACKAGE_POLICY_DENIED"
    assert result["data"]["reason"] == "target_package_unavailable"
    controller.type_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_permission_surface_exception_allows_tap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    permission = ForegroundApp(
        package="com.android.permissioncontroller",
        activity=".permission.ui.GrantPermissionsActivity",
    )
    _session, controller = _make_connected_session(
        monkeypatch,
        policy=PackagePolicy(allowed_system_surfaces=["permission_dialog"]),
        foreground=permission,
        element_package=permission.package or "",
    )

    result = await server.android_tap(Target(resource_id="button"))

    assert result["success"] is True
    controller.tap_resolved.assert_awaited_once()


@pytest.mark.asyncio
async def test_url_requires_explicit_browser_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session, controller = _make_connected_session(
        monkeypatch,
        policy=PackagePolicy(allowed_packages=["com.example"]),
    )

    denied = await server.android_open_url("https://example.com")

    assert denied["success"] is False
    assert denied["error_code"] == "PACKAGE_POLICY_DENIED"
    assert denied["data"]["reason"] == "system_surface_denied"
    controller.open_url.assert_not_awaited()

    allowed_session, _controller = _make_connected_session(
        monkeypatch,
        policy=PackagePolicy(
            allowed_packages=["com.example"],
            allowed_system_surfaces=["browser"],
        ),
    )
    allowed = await server.android_open_url("https://example.com")

    assert allowed["success"] is True
    assert allowed_session.status().policy.allowed_system_surfaces == ["browser"]


@pytest.mark.asyncio
async def test_policy_gate_covers_long_press_swipe_input_clear_launch_and_terminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session, controller = _make_connected_session(
        monkeypatch,
        policy=PackagePolicy(allowed_packages=["com.example"]),
    )

    long_press = await server.android_long_press(Target(resource_id="button"))
    swipe = await server.android_swipe(1, 1, 10, 10)
    typed = await server.android_type_text("hello")
    cleared = await server.android_clear_text()
    launched = await server.android_launch_app("com.example")
    terminated = await server.android_terminate_app("com.example")

    assert long_press["success"] is True
    assert swipe["success"] is True
    assert typed["success"] is True
    assert cleared["success"] is True
    assert launched["success"] is True
    assert terminated["success"] is True
    controller.long_press_resolved.assert_awaited_once()
    controller.swipe.assert_awaited_once_with(1, 1, 10, 10, 400)
    controller.type_text.assert_awaited_once_with("hello", None)
    controller.clear_text.assert_awaited_once_with(100, None)
    controller.launch_app.assert_awaited_once_with("com.example")
    controller.terminate_app.assert_awaited_once_with("com.example")


@pytest.mark.asyncio
async def test_policy_denies_launch_and_termination_target_packages_before_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session, controller = _make_connected_session(
        monkeypatch,
        policy=PackagePolicy(allowed_packages=["com.example"]),
    )

    launch = await server.android_launch_app("com.other")
    terminate = await server.android_terminate_app("com.other")

    assert launch["success"] is False
    assert launch["error_code"] == "PACKAGE_POLICY_DENIED"
    assert terminate["success"] is False
    assert terminate["error_code"] == "PACKAGE_POLICY_DENIED"
    controller.launch_app.assert_not_awaited()
    controller.terminate_app.assert_not_awaited()
