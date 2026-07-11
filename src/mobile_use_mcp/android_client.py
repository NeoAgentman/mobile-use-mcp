"""Minimal uiautomator2 client for screenshots, hierarchy, and text input."""

import base64
from collections.abc import Callable
from io import BytesIO
from typing import Protocol, cast

import uiautomator2  # pyright: ignore[reportMissingTypeStubs]
from PIL import Image


class AndroidDevice(Protocol):
    @property
    def info(self) -> dict[str, object]: ...

    def screenshot(self) -> Image.Image | None: ...

    def dump_hierarchy(self, compressed: bool = True) -> str: ...

    def press(self, key: str) -> bool: ...

    def set_fastinput_ime(self, enabled: bool) -> None: ...

    def send_keys(self, text: str) -> None: ...


DeviceConnector = Callable[[str], AndroidDevice]


def _default_connector(serial: str) -> AndroidDevice:
    return cast(AndroidDevice, uiautomator2.connect(serial))


class AndroidClient:
    """Lazily connects to uiautomator2 and performs one controlled reconnect."""

    def __init__(self, serial: str, connector: DeviceConnector = _default_connector):
        self.serial = serial
        self._connector = connector
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
        device = self.ensure_connected()
        device.set_fastinput_ime(True)
        try:
            device.send_keys(text)
        finally:
            device.set_fastinput_ime(False)

    def disconnect(self) -> None:
        self._device = None
