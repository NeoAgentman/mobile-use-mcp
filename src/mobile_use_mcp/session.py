"""Single-device Android session lifecycle for the local stdio server."""

import asyncio
from collections.abc import Callable

from mobile_use_mcp.controller import AndroidController
from mobile_use_mcp.devices import DeviceRegistry
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import DeviceInfo

ControllerFactory = Callable[[str], AndroidController]


class SessionManager:
    """Owns one active device and serializes all state-changing actions."""

    def __init__(
        self,
        registry: DeviceRegistry | None = None,
        controller_factory: ControllerFactory = AndroidController,
    ):
        self.registry = registry or DeviceRegistry()
        self.controller_factory = controller_factory
        self._device: DeviceInfo | None = None
        self._controller: AndroidController | None = None
        self.write_lock = asyncio.Lock()

    @property
    def device(self) -> DeviceInfo | None:
        return self._device

    def connect(self, serial: str | None = None) -> DeviceInfo:
        selected = self.registry.select(serial)
        if self._controller is not None:
            self._controller.disconnect()
        self._device = None
        self._controller = None
        controller = self.controller_factory(selected.serial)
        controller.android_client.connect()
        self._device = selected
        self._controller = controller
        return selected

    def require_controller(self) -> AndroidController:
        if self._controller is None or self._device is None:
            raise MobileUseError(
                ErrorCode.NOT_CONNECTED,
                "No Android device is connected to the MCP session.",
                "Call android_connect before using device tools.",
            )
        try:
            current = self.registry.select(self._device.serial)
        except MobileUseError as error:
            if error.code in {
                ErrorCode.DEVICE_NOT_FOUND,
                ErrorCode.DEVICE_OFFLINE,
            }:
                raise MobileUseError(
                    ErrorCode.DEVICE_DISCONNECTED,
                    f"Android device {self._device.serial!r} is no longer connected and ready.",
                    "Reconnect the device, verify `adb devices -l`, then call "
                    "android_connect again.",
                ) from error
            raise
        self._device = current
        return self._controller

    def disconnect(self) -> None:
        if self._controller is not None:
            self._controller.disconnect()
        self._controller = None
        self._device = None
