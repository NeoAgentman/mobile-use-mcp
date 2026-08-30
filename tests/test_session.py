import asyncio
from unittest.mock import Mock

import pytest

from mobile_use_mcp.config import RuntimeConfig
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import (
    DeviceInfo,
    DeviceState,
    ForegroundApp,
    PackagePolicy,
    ScreenSnapshot,
    SessionLifecycle,
)
from mobile_use_mcp.session import SessionManager


def test_session_connects_and_disconnects() -> None:
    registry = Mock()
    registry.select.return_value = DeviceInfo(serial="ABC", state=DeviceState.DEVICE)
    controller = Mock()
    manager = SessionManager(registry=registry, controller_factory=lambda _: controller)

    selected = manager.connect("ABC")

    assert selected.serial == "ABC"
    controller.android_client.connect.assert_called_once()
    assert manager.require_controller() is controller
    assert registry.select.call_count == 2

    manager.disconnect()
    controller.disconnect.assert_called_once()
    assert manager.device is None


def test_session_requires_connection() -> None:
    manager = SessionManager()

    with pytest.raises(MobileUseError) as caught:
        manager.require_controller()

    assert caught.value.code == ErrorCode.NOT_CONNECTED


def test_session_maps_lost_device_to_disconnected() -> None:
    registry = Mock()
    registry.select.side_effect = [
        DeviceInfo(serial="ABC", state=DeviceState.DEVICE),
        MobileUseError(ErrorCode.DEVICE_NOT_FOUND, "missing"),
    ]
    controller = Mock()
    manager = SessionManager(registry=registry, controller_factory=lambda _: controller)
    manager.connect("ABC")

    with pytest.raises(MobileUseError) as caught:
        manager.require_controller()

    assert caught.value.code == ErrorCode.DEVICE_DISCONNECTED


def test_failed_reconnect_does_not_leave_stale_session() -> None:
    registry = Mock()
    registry.select.side_effect = [
        DeviceInfo(serial="OLD", state=DeviceState.DEVICE),
        DeviceInfo(serial="NEW", state=DeviceState.DEVICE),
    ]
    old_controller = Mock()
    new_controller = Mock()
    new_controller.android_client.connect.side_effect = RuntimeError("connection failed")
    controllers = iter([old_controller, new_controller])
    manager = SessionManager(
        registry=registry,
        controller_factory=lambda _: next(controllers),
    )
    manager.connect("OLD")

    with pytest.raises(RuntimeError, match="connection failed"):
        manager.connect("NEW")

    old_controller.disconnect.assert_called_once()
    assert manager.device is None
    with pytest.raises(MobileUseError) as caught:
        manager.require_controller()
    assert caught.value.code == ErrorCode.NOT_CONNECTED


def test_session_caches_only_latest_snapshot() -> None:
    manager = SessionManager()
    snapshot = ScreenSnapshot(
        serial="ABC",
        width=1080,
        height=2400,
        screenshot_png=b"png",
        elements=[],
        foreground_app=ForegroundApp(),
    )

    first_id = manager.store_snapshot(snapshot)
    second_id = manager.store_snapshot(snapshot)

    assert manager.get_snapshot(second_id) is snapshot
    with pytest.raises(MobileUseError) as stale:
        manager.get_snapshot(first_id)
    assert stale.value.code == ErrorCode.SNAPSHOT_NOT_FOUND

    manager.clear_snapshot()
    with pytest.raises(MobileUseError):
        manager.get_snapshot(second_id)


def test_connection_returns_opaque_context_and_advances_generation() -> None:
    registry = Mock()
    registry.select.return_value = DeviceInfo(serial="ABC", state=DeviceState.DEVICE)
    first_controller = Mock()
    second_controller = Mock()
    controllers = iter([first_controller, second_controller])
    manager = SessionManager(registry=registry, controller_factory=lambda _: next(controllers))

    first = manager.connect("ABC")
    second = manager.connect("ABC")

    assert first.session_id.startswith("session-")
    assert second.session_id.startswith("session-")
    assert second.session_id != first.session_id
    assert second.generation > first.generation
    assert second.state == SessionLifecycle.READY
    assert manager.status().state == SessionLifecycle.READY
    first_controller.disconnect.assert_called_once()


def test_connection_carries_policy_and_reconnect_cannot_drop_it_implicitly() -> None:
    registry = Mock()
    registry.select.return_value = DeviceInfo(serial="ABC", state=DeviceState.DEVICE)
    first_controller = Mock()
    second_controller = Mock()
    controllers = iter([first_controller, second_controller])
    manager = SessionManager(
        registry=registry,
        controller_factory=lambda _: next(controllers),
    )

    first = manager.connect("ABC", policy=PackagePolicy(allowed_packages=["com.example"]))
    second = manager.connect("ABC")

    assert first.policy is not None
    assert first.policy.allowed_packages == ["com.example"]
    assert manager.policy_summary().enabled is True
    assert second.policy is not None
    assert second.policy.allowed_packages == ["com.example"]
    assert manager.status().policy.allowed_packages == ["com.example"]


def test_allow_unrestricted_is_explicit_when_reconnecting() -> None:
    registry = Mock()
    registry.select.return_value = DeviceInfo(serial="ABC", state=DeviceState.DEVICE)
    controllers = iter([Mock(), Mock()])
    manager = SessionManager(registry=registry, controller_factory=lambda _: next(controllers))

    manager.connect("ABC", allowed_packages=["com.example"])
    connection = manager.connect("ABC", allow_unrestricted=True)

    assert connection.policy is not None
    assert connection.policy.enabled is False
    assert manager.status().policy.enabled is False


@pytest.mark.asyncio
async def test_package_policy_rejects_current_and_target_before_dispatch() -> None:
    registry = Mock()
    registry.select.return_value = DeviceInfo(serial="ABC", state=DeviceState.DEVICE)
    controller = Mock()
    controller.get_foreground_app = Mock(
        return_value=ForegroundApp(package="com.example", activity=".Main")
    )
    manager = SessionManager(registry=registry, controller_factory=lambda _: controller)
    manager.connect("ABC", allowed_packages=["com.example"])

    await manager.check_package_policy(
        action="tap",
        controller=controller,
        target_package="com.example",
    )
    with pytest.raises(MobileUseError) as denied:
        await manager.check_package_policy(
            action="tap",
            controller=controller,
            target_package="com.other",
        )

    assert denied.value.code == ErrorCode.PACKAGE_POLICY_DENIED
    assert denied.value.data["reason"] == "target_package_denied"

    with pytest.raises(MobileUseError) as unknown_target:
        await manager.check_package_policy(
            action="tap",
            controller=controller,
            target_package_required=True,
        )

    assert unknown_target.value.code == ErrorCode.PACKAGE_POLICY_DENIED
    assert unknown_target.value.data["reason"] == "target_package_unavailable"


@pytest.mark.asyncio
async def test_package_policy_foreground_read_failure_fails_closed() -> None:
    registry = Mock()
    registry.select.return_value = DeviceInfo(serial="ABC", state=DeviceState.DEVICE)
    controller = Mock()
    controller.get_foreground_app = Mock(side_effect=RuntimeError("adapter detail"))
    manager = SessionManager(registry=registry, controller_factory=lambda _: controller)
    manager.connect("ABC", allowed_packages=["com.example"])

    with pytest.raises(MobileUseError) as failed:
        await manager.check_package_policy(action="press_key", controller=controller)

    assert failed.value.code == ErrorCode.PACKAGE_POLICY_FOREGROUND_UNAVAILABLE
    assert "adapter detail" not in str(failed.value)


def test_partial_connection_failure_rolls_back_new_controller_and_marks_failed() -> None:
    registry = Mock()
    registry.select.return_value = DeviceInfo(serial="ABC", state=DeviceState.DEVICE)
    controller = Mock()
    controller.android_client.connect.side_effect = RuntimeError("transport unavailable")
    manager = SessionManager(registry=registry, controller_factory=lambda _: controller)

    with pytest.raises(RuntimeError, match="transport unavailable"):
        manager.connect("ABC")

    assert manager.controller is None
    assert manager.device is None
    assert manager.status().state == SessionLifecycle.FAILED
    assert manager.status().last_error is not None
    assert "transport unavailable" not in manager.status().message
    controller.disconnect.assert_called_once()


def test_device_loss_is_observable_as_degraded() -> None:
    registry = Mock()
    registry.select.side_effect = [
        DeviceInfo(serial="ABC", state=DeviceState.DEVICE),
        MobileUseError(ErrorCode.DEVICE_OFFLINE, "offline"),
    ]
    controller = Mock()
    manager = SessionManager(registry=registry, controller_factory=lambda _: controller)
    manager.connect("ABC")

    with pytest.raises(MobileUseError) as caught:
        manager.require_controller()

    assert caught.value.code == ErrorCode.DEVICE_DISCONNECTED
    status = manager.status()
    assert status.state == SessionLifecycle.DEGRADED
    assert status.device is not None
    assert status.connected is False


@pytest.mark.asyncio
async def test_async_close_uses_idempotent_disconnect_path() -> None:
    registry = Mock()
    registry.select.return_value = DeviceInfo(serial="ABC", state=DeviceState.DEVICE)
    controller = Mock()
    manager = SessionManager(registry=registry, controller_factory=lambda _: controller)
    manager.connect("ABC")

    closed = await manager.close()
    repeated = manager.disconnect()

    assert closed.success is True
    assert closed.state == SessionLifecycle.DISCONNECTED
    assert repeated.success is True
    assert repeated.state == SessionLifecycle.DISCONNECTED
    controller.disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_timed_out_observation_cannot_publish_a_late_snapshot() -> None:
    manager = SessionManager(
        config=RuntimeConfig(operation_timeout_seconds=0.02, queue_timeout_seconds=1)
    )
    snapshot = ScreenSnapshot(
        serial="ABC",
        width=1080,
        height=2400,
        screenshot_png=b"png",
        elements=[],
        foreground_app=ForegroundApp(),
    )
    committed = False

    async def slow_observation(_context: object) -> None:
        nonlocal committed
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            # A late adapter completion must be rejected by the session's
            # operation context rather than replacing a newer observation.
            await asyncio.sleep(0)
            manager.store_snapshot(snapshot)
            committed = True

    with pytest.raises(MobileUseError) as caught:
        await manager.run_operation("snapshot", slow_observation, retry_safe=True, mutating=False)

    await asyncio.sleep(0.01)
    assert caught.value.code == ErrorCode.TIMEOUT
    assert committed is False
    with pytest.raises(MobileUseError) as missing:
        manager.get_snapshot("s-late")
    assert missing.value.code == ErrorCode.SNAPSHOT_NOT_FOUND
