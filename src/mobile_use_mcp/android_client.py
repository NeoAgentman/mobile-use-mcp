"""Minimal uiautomator2 client for screenshots, hierarchy, and text input."""

import base64
import time
from collections.abc import Callable
from contextlib import suppress
from io import BytesIO
from threading import Lock
from typing import Protocol, cast

import uiautomator2  # pyright: ignore[reportMissingTypeStubs]
from PIL import Image

from mobile_use_mcp.config import RuntimeConfig


class AndroidDevice(Protocol):
    @property
    def info(self) -> dict[str, object]: ...

    def screenshot(self) -> Image.Image | None: ...

    def dump_hierarchy(self, compressed: bool = True) -> str: ...

    def press(self, key: str) -> bool: ...

    def set_input_ime(self, enabled: bool = True) -> None: ...

    def send_keys(self, text: str) -> None: ...

    def clear_text(self) -> None: ...


DeviceConnector = Callable[[str], AndroidDevice]


def _close_device(device: AndroidDevice | None) -> None:
    """Stop concrete uiautomator2 transports without requiring its private API."""

    if device is None:
        return
    close = getattr(device, "close", None)
    if callable(close):
        with suppress(Exception):
            close()
        return
    # Current uiautomator2 exposes ``stop_uiautomator`` rather than ``close``.
    # ``wait=False`` avoids a second unbounded health-poll during cancellation;
    # the adb socket timeout and process owner already bound the operation.
    stop = getattr(device, "stop_uiautomator", None)
    if callable(stop):
        with suppress(Exception):
            stop(wait=False)


def _default_connector(serial: str, config: RuntimeConfig | None = None) -> AndroidDevice:
    """Connect uiautomator2 through the configured ADB endpoint.

    Passing an ``adbutils`` device object is important here: uiautomator2's
    string connector reads process-global ADB environment variables, which
    would otherwise allow discovery and the controller to talk to different
    ADB servers in the same process.
    """

    if config is None:
        return cast(AndroidDevice, uiautomator2.connect(serial))
    from adbutils import AdbClient  # pyright: ignore[reportMissingTypeStubs]

    adb_device = AdbClient(
        host=config.adb_host,
        port=config.adb_port,
        socket_timeout=config.subprocess_timeout_seconds,
    ).device(serial=serial)
    return cast(AndroidDevice, uiautomator2.connect(adb_device))


class AndroidClient:
    """Lazily connects to uiautomator2 and performs one controlled reconnect."""

    def __init__(
        self,
        serial: str,
        connector: DeviceConnector | None = None,
        *,
        config: RuntimeConfig | None = None,
    ):
        self.serial = serial
        self.config = config or RuntimeConfig.defaults()
        connector_impl: DeviceConnector
        if connector is not None:
            connector_impl = connector
        elif config is not None:

            def configured_connector(value: str) -> AndroidDevice:
                return _default_connector(value, self.config)

            connector_impl = configured_connector
        else:
            connector_impl = _default_connector
        self._connector = connector_impl
        self._device: AndroidDevice | None = None
        self._state_lock = Lock()
        self._generation = 0

    def connect(self) -> None:
        with self._state_lock:
            generation = self._generation
        device = self._connector(self.serial)
        _ = device.info
        # A timed-out/cancelled connect can finish after disconnect has
        # already released the adapter.  Do not publish that stale device back
        # into the client, and close it when the concrete adapter supports it.
        with self._state_lock:
            stale = generation != self._generation
            if not stale:
                self._device = device
        if stale:
            _close_device(device)
            raise RuntimeError("Android connection was closed while it started")

    def ensure_connected(self) -> AndroidDevice:
        with self._state_lock:
            device = self._device
        if device is None:
            self.connect()
            with self._state_lock:
                device = self._device
        assert device is not None
        try:
            _ = device.info
        except Exception:
            with self._state_lock:
                if self._device is device:
                    self._device = None
            self.connect()
            with self._state_lock:
                device = self._device
        assert device is not None
        return device

    def get_screen(self) -> tuple[bytes, str, int, int]:
        """Capture the legacy screenshot-plus-hierarchy pair."""

        screenshot, width, height = self.get_screenshot()
        hierarchy = self.get_hierarchy()
        return screenshot, hierarchy, width, height

    def get_screenshot(self) -> tuple[bytes, int, int]:
        """Capture native pixels without requesting accessibility hierarchy."""

        device = self.ensure_connected()
        screenshot = device.screenshot()
        if screenshot is None:
            raise RuntimeError("uiautomator2 failed to capture a screenshot")
        buffer = BytesIO()
        screenshot.save(buffer, format="PNG")
        return buffer.getvalue(), screenshot.width, screenshot.height

    def get_hierarchy(self) -> str:
        """Read the accessibility hierarchy as a separate optional component."""

        return self.ensure_connected().dump_hierarchy(compressed=True)

    def get_screenshot_base64(self) -> str:
        screenshot, _, _, _ = self.get_screen()
        return base64.b64encode(screenshot).decode("ascii")

    def press_key(self, key: str) -> bool:
        return bool(self.ensure_connected().press(key))

    def send_text(self, text: str) -> None:
        """Send Unicode text and restore the user's input method after it settles."""

        device = self.ensure_connected()
        try:
            device.send_keys(text)
            # FastInputIME broadcasts asynchronously on some Android/HarmonyOS builds.
            # Disabling it immediately can drop the beginning of the text.
            time.sleep(0.3)
        finally:
            device.set_input_ime(False)

    def clear_text(self) -> None:
        """Clear the focused field using uiautomator2's Unicode-aware input method."""

        device = self.ensure_connected()
        try:
            device.clear_text()
            time.sleep(0.2)
        finally:
            device.set_input_ime(False)

    def disconnect(self) -> None:
        with self._state_lock:
            self._generation += 1
            device = self._device
            self._device = None
        # uiautomator2 currently exposes ``close`` on its concrete device in
        # some releases but not in its public protocol.  Close when present so
        # doctor probes and normal session shutdown do not retain a socket;
        # lightweight fakes remain compatible because this is optional.
        _close_device(device)
