"""Deterministic Android operations used by MCP tool handlers."""

import asyncio
from collections.abc import Callable
from typing import Protocol, cast
from urllib.parse import urlparse

from adbutils import AdbClient  # pyright: ignore[reportMissingTypeStubs]

from mobile_use_mcp.android_client import AndroidClient
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import ForegroundApp, ScreenSnapshot, Target
from mobile_use_mcp.selectors import resolve_target
from mobile_use_mcp.snapshot import parse_hierarchy


class RunningApp(Protocol):
    package: str
    activity: str


class ADBDevice(Protocol):
    def click(self, x: int, y: int, display_id: int | None = None) -> None: ...

    def swipe(self, sx: int, sy: int, ex: int, ey: int, duration: float = 1.0) -> None: ...

    def keyevent(self, key_code: int | str) -> None: ...

    def app_start(self, package_name: str, activity: str | None = None) -> None: ...

    def app_stop(self, package_name: str) -> None: ...

    def open_browser(self, url: str) -> None: ...

    def app_current(self) -> RunningApp: ...

    def shell(
        self,
        cmdargs: list[str],
        stream: bool = False,
        timeout: float | None = 600,
        encoding: str | None = "utf-8",
        rstrip: bool = True,
    ) -> str | bytes | object: ...


ADBConnector = Callable[[str], ADBDevice]


def _default_adb_connector(serial: str) -> ADBDevice:
    return cast(ADBDevice, AdbClient(host="127.0.0.1", port=5037).device(serial=serial))


class AndroidController:
    """Combines ADB actions with uiautomator2 observation and text input."""

    ALLOWED_KEYS = {
        "back": "BACK",
        "home": "HOME",
        "enter": "ENTER",
        "delete": "DEL",
        "tab": "TAB",
        "menu": "MENU",
        "volume_up": "VOLUME_UP",
        "volume_down": "VOLUME_DOWN",
    }

    def __init__(
        self,
        serial: str,
        *,
        android_client: AndroidClient | None = None,
        adb_connector: ADBConnector = _default_adb_connector,
    ):
        self.serial = serial
        self.android_client = android_client or AndroidClient(serial)
        self.adb_device = adb_connector(serial)

    async def snapshot(
        self,
        *,
        interactive_only: bool = False,
        max_elements: int = 200,
        max_text_length: int = 500,
    ) -> ScreenSnapshot:
        screenshot, hierarchy, width, height = await asyncio.to_thread(
            self.android_client.get_screen
        )
        elements = parse_hierarchy(
            hierarchy,
            interactive_only=interactive_only,
            max_elements=max_elements,
            max_text_length=max_text_length,
        )
        foreground = await self.get_foreground_app()
        return ScreenSnapshot(
            serial=self.serial,
            width=width,
            height=height,
            screenshot_png=screenshot,
            elements=elements,
            foreground_app=foreground,
        )

    async def tap(self, target: Target) -> dict[str, object]:
        snapshot = await self.snapshot()
        resolved = resolve_target(
            target,
            snapshot.elements,
            screen_width=snapshot.width,
            screen_height=snapshot.height,
        )
        x, y = resolved.bounds.center
        await asyncio.to_thread(self.adb_device.click, x, y)
        return {
            "x": x,
            "y": y,
            "selector": resolved.selector,
            "attempts": [attempt.model_dump() for attempt in resolved.attempts],
        }

    async def long_press(self, target: Target, duration_ms: int = 1_000) -> dict[str, object]:
        snapshot = await self.snapshot()
        resolved = resolve_target(
            target,
            snapshot.elements,
            screen_width=snapshot.width,
            screen_height=snapshot.height,
        )
        x, y = resolved.bounds.center
        await asyncio.to_thread(self.adb_device.swipe, x, y, x, y, duration_ms / 1_000)
        return {
            "x": x,
            "y": y,
            "duration_ms": duration_ms,
            "selector": resolved.selector,
            "attempts": [attempt.model_dump() for attempt in resolved.attempts],
        }

    async def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 400,
    ) -> None:
        snapshot = await self.snapshot(interactive_only=True, max_elements=1)
        for name, x, y in (("start", start_x, start_y), ("end", end_x, end_y)):
            if not 0 <= x < snapshot.width or not 0 <= y < snapshot.height:
                raise MobileUseError(
                    ErrorCode.INVALID_COORDINATES,
                    f"Swipe {name} coordinate ({x}, {y}) is outside "
                    f"the {snapshot.width}x{snapshot.height} screen.",
                    "Call android_snapshot and choose coordinates inside the current screen.",
                )
        await asyncio.to_thread(
            self.adb_device.swipe,
            start_x,
            start_y,
            end_x,
            end_y,
            duration_ms / 1_000,
        )

    async def type_text(self, text: str) -> str:
        try:
            await asyncio.to_thread(self.android_client.send_text, text)
            return "uiautomator2"
        except Exception:
            await asyncio.to_thread(self.adb_device.shell, ["input", "text", text])
            return "adb"

    async def clear_text(self, characters: int = 100) -> None:
        for _ in range(characters):
            await asyncio.to_thread(self.adb_device.keyevent, "DEL")

    async def press_key(self, key: str) -> None:
        normalized = key.casefold()
        key_code = self.ALLOWED_KEYS.get(normalized)
        if key_code is None:
            allowed = ", ".join(sorted(self.ALLOWED_KEYS))
            raise MobileUseError(
                ErrorCode.OPERATION_FAILED,
                f"Unsupported Android key {key!r}. Allowed keys: {allowed}.",
            )
        await asyncio.to_thread(self.adb_device.keyevent, key_code)

    async def launch_app(
        self,
        package: str,
        *,
        retries: int = 3,
        timeout_seconds: float = 15,
    ) -> ForegroundApp:
        for attempt in range(retries):
            await asyncio.to_thread(self.adb_device.app_start, package)
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            while asyncio.get_running_loop().time() < deadline:
                foreground = await self.get_foreground_app()
                if foreground.package == package:
                    return foreground
                await asyncio.sleep(0.5)
            if attempt + 1 < retries:
                await asyncio.sleep(0.5)
        raise MobileUseError(
            ErrorCode.TIMEOUT,
            f"App {package!r} did not reach the foreground after {retries} attempts.",
            "Check that the package is installed with android_list_apps.",
        )

    async def terminate_app(self, package: str) -> None:
        await asyncio.to_thread(self.adb_device.app_stop, package)

    async def open_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MobileUseError(
                ErrorCode.OPERATION_FAILED,
                "Only absolute http:// and https:// URLs are supported.",
            )
        await asyncio.to_thread(self.adb_device.open_browser, url)

    async def get_foreground_app(self) -> ForegroundApp:
        try:
            current = await asyncio.to_thread(self.adb_device.app_current)
        except Exception:
            return ForegroundApp()
        return ForegroundApp(package=current.package or None, activity=current.activity or None)

    async def list_apps(self, query: str | None = None, limit: int = 100) -> list[str]:
        output = await asyncio.to_thread(
            self.adb_device.shell,
            ["pm", "list", "packages", "-3"],
        )
        if not isinstance(output, str):
            raise MobileUseError(ErrorCode.OPERATION_FAILED, "ADB returned invalid package data.")
        packages = sorted(
            line.removeprefix("package:").strip()
            for line in output.splitlines()
            if line.startswith("package:")
        )
        if query:
            normalized = query.casefold()
            packages = [package for package in packages if normalized in package.casefold()]
        return packages[:limit]

    def disconnect(self) -> None:
        self.android_client.disconnect()
