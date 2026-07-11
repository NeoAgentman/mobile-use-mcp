"""Single-device Android session lifecycle for the local stdio server."""

import asyncio
from collections.abc import Callable
from uuid import uuid4

from mobile_use_mcp.controller import AndroidController
from mobile_use_mcp.devices import DeviceRegistry
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import DeviceInfo, ScreenSnapshot

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
        self._snapshot_id: str | None = None
        self._snapshot: ScreenSnapshot | None = None
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
        self.clear_snapshot()
        controller = self.controller_factory(selected.serial)
        controller.android_client.connect()
        self._device = selected
        self._controller = controller
        return selected

    def store_snapshot(self, snapshot: ScreenSnapshot) -> str:
        """Replace the one-entry snapshot cache and return its opaque identifier."""

        self._snapshot_id = f"s-{uuid4().hex}"
        self._snapshot = snapshot
        return self._snapshot_id

    def get_snapshot(self, snapshot_id: str) -> ScreenSnapshot:
        """Return the cached snapshot or reject an expired/unknown identifier."""

        if self._snapshot_id != snapshot_id or self._snapshot is None:
            raise MobileUseError(
                ErrorCode.SNAPSHOT_NOT_FOUND,
                f"Android snapshot {snapshot_id!r} is no longer available.",
                "Call android_snapshot to capture the current screen and use its new snapshot_id.",
            )
        return self._snapshot

    def clear_snapshot(self) -> None:
        self._snapshot_id = None
        self._snapshot = None

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
        self.clear_snapshot()
