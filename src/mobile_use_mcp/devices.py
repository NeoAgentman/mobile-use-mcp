"""Safe Android device discovery through the ADB executable."""

import shutil
import subprocess
from collections.abc import Sequence

from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import DeviceInfo, DeviceState


def parse_adb_devices(output: str) -> list[DeviceInfo]:
    """Parse `adb devices -l` without accepting arbitrary shell input."""

    devices: list[DeviceInfo] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices attached") or line.startswith("*"):
            continue

        fields = line.split()
        if len(fields) < 2:
            continue

        serial, raw_state, *metadata_fields = fields
        metadata = {
            key: value
            for field in metadata_fields
            if ":" in field
            for key, value in [field.split(":", 1)]
        }
        state = DeviceState(raw_state) if raw_state in DeviceState else DeviceState.UNKNOWN
        devices.append(
            DeviceInfo(
                serial=serial,
                state=state,
                model=metadata.get("model"),
                product=metadata.get("product"),
                transport_id=metadata.get("transport_id"),
            )
        )
    return devices


class DeviceRegistry:
    """Lists and selects Android devices with explicit ambiguity handling."""

    def __init__(self, adb_path: str | None = None):
        self.adb_path = adb_path or shutil.which("adb") or "adb"

    def list_devices(self) -> list[DeviceInfo]:
        try:
            result = subprocess.run(
                [self.adb_path, "devices", "-l"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError as error:
            raise MobileUseError(
                ErrorCode.ADB_UNAVAILABLE,
                "ADB executable was not found.",
                "Install Android platform-tools and ensure adb is available on PATH.",
            ) from error
        except subprocess.TimeoutExpired as error:
            raise MobileUseError(
                ErrorCode.TIMEOUT,
                "Timed out while listing Android devices.",
                "Restart the ADB server and try again.",
            ) from error

        if result.returncode != 0:
            raise MobileUseError(
                ErrorCode.ADB_UNAVAILABLE,
                "ADB failed while listing devices.",
                "Run `adb devices -l` manually and resolve the reported ADB error.",
            )
        return parse_adb_devices(result.stdout)

    def select(self, serial: str | None = None) -> DeviceInfo:
        devices = self.list_devices()
        if serial:
            return self._select_serial(devices, serial)

        online = [device for device in devices if device.state == DeviceState.DEVICE]
        if not online:
            raise MobileUseError(
                ErrorCode.DEVICE_NOT_FOUND,
                "No authorized online Android device was found.",
                "Connect a device with USB debugging enabled and accept the authorization prompt.",
            )
        if len(online) > 1:
            serials = ", ".join(device.serial for device in online)
            raise MobileUseError(
                ErrorCode.MULTIPLE_DEVICES,
                f"Multiple online Android devices were found: {serials}.",
                "Call android_connect with the desired device serial.",
            )
        return online[0]

    @staticmethod
    def _select_serial(devices: Sequence[DeviceInfo], serial: str) -> DeviceInfo:
        device = next((candidate for candidate in devices if candidate.serial == serial), None)
        if device is None:
            raise MobileUseError(
                ErrorCode.DEVICE_NOT_FOUND,
                f"Android device {serial!r} was not found.",
                "Call android_list_devices and use a serial from the current result.",
            )
        if device.state == DeviceState.UNAUTHORIZED:
            raise MobileUseError(
                ErrorCode.DEVICE_UNAUTHORIZED,
                f"Android device {serial!r} is unauthorized.",
                "Unlock the device and accept the USB debugging authorization prompt.",
            )
        if device.state == DeviceState.OFFLINE:
            raise MobileUseError(
                ErrorCode.DEVICE_OFFLINE,
                f"Android device {serial!r} is offline.",
                "Reconnect the device or restart the ADB server.",
            )
        if device.state != DeviceState.DEVICE:
            raise MobileUseError(
                ErrorCode.DEVICE_NOT_FOUND,
                f"Android device {serial!r} is not ready (state={device.state}).",
                "Call android_list_devices after resolving the device state.",
            )
        return device
