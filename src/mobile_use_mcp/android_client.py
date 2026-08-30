"""Minimal uiautomator2 client for screenshots, hierarchy, and text input."""

import base64
import time
from collections.abc import Callable
from contextlib import suppress
from io import BytesIO
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

    adb_device = AdbClient(host=config.adb_host, port=config.adb_port).device(serial=serial)
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

    def connect(self) -> None:
        device = self._connector(self.serial)
        _ = device.info
        self._device = device

    def ensure_connected(self) -> AndroidDevice:
        if self._device is None:
            self.connect()
        assert self._device is not None
        try:
            _ = self._device.info
        except Exception:
            self._device = None
            self.connect()
        assert self._device is not None
        return self._device

    def get_screen(self) -> tuple[bytes, str, int, int]:
        device = self.ensure_connected()
        screenshot = device.screenshot()
        if screenshot is None:
            raise RuntimeError("uiautomator2 failed to capture a screenshot")
        hierarchy = device.dump_hierarchy(compressed=True)
        buffer = BytesIO()
        screenshot.save(buffer, format="PNG")
        return buffer.getvalue(), hierarchy, screenshot.width, screenshot.height

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
        device = self._device
        self._device = None
        # uiautomator2 currently exposes ``close`` on its concrete device in
        # some releases but not in its public protocol.  Close when present so
        # doctor probes and normal session shutdown do not retain a socket;
        # lightweight fakes remain compatible because this is optional.
        close = getattr(device, "close", None)
        if callable(close):
            with suppress(Exception):
                close()
