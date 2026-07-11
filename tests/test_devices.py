from unittest.mock import Mock, patch

import pytest

from mobile_use_mcp.devices import DeviceRegistry, parse_adb_devices
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import DeviceState

ADB_OUTPUT = """List of devices attached
emulator-5554 device product:sdk_phone model:Pixel_9 transport_id:1
ABC123 unauthorized usb:1-1 transport_id:2
OFF456 offline transport_id:3
"""


def test_parse_adb_devices() -> None:
    devices = parse_adb_devices(ADB_OUTPUT)

    assert [device.serial for device in devices] == ["emulator-5554", "ABC123", "OFF456"]
    assert devices[0].state == DeviceState.DEVICE
    assert devices[0].model == "Pixel_9"
    assert devices[1].state == DeviceState.UNAUTHORIZED


@patch("mobile_use_mcp.devices.subprocess.run")
def test_registry_lists_devices(mock_run: Mock) -> None:
    mock_run.return_value = Mock(returncode=0, stdout=ADB_OUTPUT)

    devices = DeviceRegistry(adb_path="adb").list_devices()

    assert len(devices) == 3
    mock_run.assert_called_once_with(
        ["adb", "devices", "-l"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_registry_selects_only_online_device() -> None:
    registry = DeviceRegistry()
    registry.list_devices = lambda: parse_adb_devices(ADB_OUTPUT)  # type: ignore[method-assign]

    assert registry.select().serial == "emulator-5554"


@pytest.mark.parametrize(
    ("serial", "code"),
    [("ABC123", ErrorCode.DEVICE_UNAUTHORIZED), ("OFF456", ErrorCode.DEVICE_OFFLINE)],
)
def test_registry_reports_unusable_explicit_device(serial: str, code: ErrorCode) -> None:
    registry = DeviceRegistry()
    registry.list_devices = lambda: parse_adb_devices(ADB_OUTPUT)  # type: ignore[method-assign]

    with pytest.raises(MobileUseError) as caught:
        registry.select(serial)

    assert caught.value.code == code


def test_registry_rejects_multiple_implicit_devices() -> None:
    registry = DeviceRegistry()
    registry.list_devices = lambda: parse_adb_devices(  # type: ignore[method-assign]
        "List of devices attached\nA device\nB device\n"
    )

    with pytest.raises(MobileUseError) as caught:
        registry.select()

    assert caught.value.code == ErrorCode.MULTIPLE_DEVICES
