from unittest.mock import Mock

import pytest

from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import DeviceInfo, DeviceState
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
