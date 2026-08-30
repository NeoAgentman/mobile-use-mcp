"""Safe Android device discovery through the ADB executable."""

import subprocess
from collections.abc import Sequence

from mobile_use_mcp.config import RuntimeConfig
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

    def __init__(
        self,
        adb_path: str | None = None,
        *,
        adb_host: str | None = None,
        adb_port: int | None = None,
        command_timeout: float | None = None,
        config: RuntimeConfig | None = None,
    ):
        """Create a registry using one immutable runtime configuration.

        The explicit arguments remain for source compatibility with the
        original core API.  New callers should pass ``config`` so discovery,
        controller, and recording all share the exact same endpoint.
        """

        runtime = config or RuntimeConfig.defaults()
        self.config = runtime
        self.adb_path = adb_path if adb_path is not None else runtime.adb_executable
        self.adb_host = adb_host if adb_host is not None else runtime.adb_host
        self.adb_port = adb_port if adb_port is not None else runtime.adb_port
        self.command_timeout = (
            command_timeout
            if command_timeout is not None
            else runtime.subprocess_timeout_seconds
        )

    def _adb_command(self, *arguments: str) -> list[str]:
        command = [self.adb_path]
        # The default command intentionally stays byte-for-byte compatible
        # with the original local stdio behavior.  Custom endpoints are
        # explicit argv flags and never become shell input.
        if self.adb_host != "127.0.0.1" or self.adb_port != 5037:
            command.extend(["-H", self.adb_host, "-P", str(self.adb_port)])
        command.extend(arguments)
        return command

    def check_adb(self) -> str:
        """Run a bounded, value-free ADB availability probe."""

        try:
            result = subprocess.run(
                self._adb_command("version"),
                check=False,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
            )
        except FileNotFoundError as error:
            raise MobileUseError(
                ErrorCode.ADB_UNAVAILABLE,
                "ADB executable was not found.",
                "Install Android platform-tools or configure a valid ADB executable.",
            ) from error
        except subprocess.TimeoutExpired as error:
            raise MobileUseError(
                ErrorCode.TIMEOUT,
                "Timed out while checking ADB availability.",
                "Check the configured ADB endpoint and restart the ADB server.",
            ) from error
        except OSError as error:
            raise MobileUseError(
                ErrorCode.ADB_UNAVAILABLE,
                "The configured ADB executable could not be started.",
                "Check the configured ADB executable permissions.",
            ) from error
        if result.returncode != 0:
            raise MobileUseError(
                ErrorCode.ADB_UNAVAILABLE,
                "ADB did not respond successfully.",
                "Check the configured ADB endpoint and run `adb version` manually.",
            )
        return "available"

    def shell(self, serial: str, arguments: Sequence[str]) -> str:
        """Run a bounded parameterized shell probe for one explicit serial."""

        command = self._adb_command("-s", serial, "shell", *arguments)
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
            )
        except FileNotFoundError as error:
            raise MobileUseError(
                ErrorCode.ADB_UNAVAILABLE,
                "ADB executable was not found.",
                "Install Android platform-tools or configure a valid ADB executable.",
            ) from error
        except subprocess.TimeoutExpired as error:
            raise MobileUseError(
                ErrorCode.TIMEOUT,
                "Timed out while running the Android capability probe.",
                "Check the selected device and ADB endpoint, then retry.",
            ) from error
        if result.returncode != 0:
            raise MobileUseError(
                ErrorCode.OPERATION_FAILED,
                "ADB could not run the Android capability probe.",
                "Verify that the selected device is online and authorized.",
            )
        output_size = len(result.stdout.encode("utf-8", errors="replace"))
        if output_size > self.config.subprocess_max_output_bytes:
            raise MobileUseError(
                ErrorCode.OPERATION_FAILED,
                "ADB returned more data than the configured subprocess budget.",
                "Increase the subprocess output budget only when the probe requires it.",
            )
        return result.stdout

    def list_devices(self) -> list[DeviceInfo]:
        try:
            result = subprocess.run(
                self._adb_command("devices", "-l"),
                check=False,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
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
        except OSError as error:
            raise MobileUseError(
                ErrorCode.ADB_UNAVAILABLE,
                "The configured ADB executable could not be started.",
                "Check the configured ADB executable permissions.",
            ) from error

        if result.returncode != 0:
            raise MobileUseError(
                ErrorCode.ADB_UNAVAILABLE,
                "ADB failed while listing devices.",
                "Run `adb devices -l` manually and resolve the reported ADB error.",
            )
        output_size = len(result.stdout.encode("utf-8", errors="replace"))
        if output_size > self.config.subprocess_max_output_bytes:
            raise MobileUseError(
                ErrorCode.OPERATION_FAILED,
                "ADB returned more data than the configured subprocess budget.",
                "Increase the subprocess output budget only when device discovery requires it.",
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
