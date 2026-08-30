"""Deterministic Android operations used by MCP tool handlers."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Protocol, cast
from urllib.parse import urlparse

from adbutils import AdbClient  # pyright: ignore[reportMissingTypeStubs]

from mobile_use_mcp.android_client import AndroidClient
from mobile_use_mcp.config import RuntimeConfig
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import (
    AppLaunchAttempt,
    AppLaunchResult,
    ForegroundApp,
    ScreenSnapshot,
    Target,
)
from mobile_use_mcp.processes import ProcessTimeoutError, run_blocking
from mobile_use_mcp.recording import RecordingADBDevice, RecordingManager
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


def _default_adb_connector(
    serial: str,
    config: RuntimeConfig | None = None,
) -> ADBDevice:
    runtime = config or RuntimeConfig.defaults()
    return cast(
        ADBDevice,
        AdbClient(
            host=runtime.adb_host,
            port=runtime.adb_port,
            socket_timeout=runtime.subprocess_timeout_seconds,
        ).device(serial=serial),
    )


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
        adb_connector: ADBConnector | None = None,
        config: RuntimeConfig | None = None,
    ):
        self.serial = serial
        self.config = config or RuntimeConfig.defaults()
        self.android_client = android_client or AndroidClient(serial, config=self.config)
        self.adb_device = (
            adb_connector(serial)
            if adb_connector is not None
            else _default_adb_connector(serial, self.config)
        )
        self.recording = RecordingManager(
            serial,
            cast(RecordingADBDevice, self.adb_device),
            config=self.config,
        )

    def _close_adapters(self) -> None:
        """Interrupt a blocking library call when its owner is cancelled."""

        with suppress(Exception):
            self.android_client.disconnect()
        close = getattr(self.adb_device, "close", None)
        if callable(close):
            with suppress(Exception):
                close()

    async def _call_blocking(
        self,
        function: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Invoke adb/uiautomator2 under one finite, interruptible boundary."""

        return await run_blocking(
            function,
            *args,
            timeout_seconds=self.config.subprocess_timeout_seconds,
            on_abort=self._close_adapters,
            **kwargs,
        )

    async def snapshot(
        self,
        *,
        interactive_only: bool = False,
        max_elements: int | None = 200,
        max_text_length: int = 500,
    ) -> ScreenSnapshot:
        screenshot, hierarchy, width, height = await self._call_blocking(
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
        await self._call_blocking(self.adb_device.click, x, y)
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
        await self._call_blocking(self.adb_device.swipe, x, y, x, y, duration_ms / 1_000)
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
        await self._call_blocking(
            self.adb_device.swipe,
            start_x,
            start_y,
            end_x,
            end_y,
            duration_ms / 1_000,
        )

    async def focus(self, target: Target, timeout_seconds: float = 2) -> str:
        """Tap a target and verify focus when it has a semantic selector."""

        details = await self.tap(target)
        if not target.resource_id and not target.text:
            return str(details["selector"])

        verification_target = Target(
            resource_id=target.resource_id,
            resource_id_index=target.resource_id_index,
            text=target.text,
            text_index=target.text_index,
        )
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            snapshot = await self.snapshot()
            try:
                resolved = resolve_target(
                    verification_target,
                    snapshot.elements,
                    screen_width=snapshot.width,
                    screen_height=snapshot.height,
                )
            except MobileUseError:
                await asyncio.sleep(0.1)
                continue
            if resolved.element and resolved.element.focused:
                return resolved.selector
            await asyncio.sleep(0.1)
        raise MobileUseError(
            ErrorCode.OPERATION_FAILED,
            "The requested Android input target did not become focused.",
            "Call android_snapshot, verify the input element, and try a different selector.",
        )

    async def type_text(self, text: str, target: Target | None = None) -> str:
        if target is not None:
            await self.focus(target)
        try:
            await self._call_blocking(self.android_client.send_text, text)
            return "uiautomator2"
        except ProcessTimeoutError:
            raise
        except Exception:
            await self._call_blocking(
                self.adb_device.shell,
                ["input", "text", text],
                timeout=self.config.subprocess_timeout_seconds,
            )
            return "adb"

    async def clear_text(self, characters: int = 100, target: Target | None = None) -> str:
        if target is not None:
            await self.focus(target)
        try:
            await self._call_blocking(self.android_client.clear_text)
            return "uiautomator2"
        except ProcessTimeoutError:
            raise
        except Exception:
            for _ in range(characters):
                await self._call_blocking(self.adb_device.keyevent, "DEL")
            return "adb"

    async def press_key(self, key: str) -> None:
        normalized = key.casefold()
        key_code = self.ALLOWED_KEYS.get(normalized)
        if key_code is None:
            allowed = ", ".join(sorted(self.ALLOWED_KEYS))
            raise MobileUseError(
                ErrorCode.OPERATION_FAILED,
                f"Unsupported Android key {key!r}. Allowed keys: {allowed}.",
            )
        await self._call_blocking(self.adb_device.keyevent, key_code)

    async def launch_app(
        self,
        package: str,
        *,
        retries: int = 3,
        timeout_seconds: float = 15,
    ) -> AppLaunchResult:
        attempts: list[AppLaunchAttempt] = []
        last_foreground = ForegroundApp()
        for attempt_index in range(1, retries + 1):
            try:
                await self._call_blocking(self.adb_device.app_start, package)
            except Exception as error:
                raise MobileUseError(
                    ErrorCode.OPERATION_FAILED,
                    f"Android could not start package {package!r}.",
                    "Check that the exact package is installed with android_list_apps.",
                    data={"stage": "start_command", "attempt": attempt_index},
                ) from error
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            polls = 0
            while asyncio.get_running_loop().time() < deadline:
                polls += 1
                last_foreground = await self.get_foreground_app()
                if last_foreground.package == package:
                    attempts.append(
                        AppLaunchAttempt(
                            attempt=attempt_index,
                            outcome="ready",
                            polls=polls,
                            foreground_app=last_foreground,
                        )
                    )
                    return AppLaunchResult(foreground_app=last_foreground, attempts=attempts)
                if last_foreground.package is not None:
                    attempts.append(
                        AppLaunchAttempt(
                            attempt=attempt_index,
                            outcome="blocked_by_other_app",
                            polls=polls,
                            foreground_app=last_foreground,
                        )
                    )
                    break
                await asyncio.sleep(0.5)
            else:
                attempts.append(
                    AppLaunchAttempt(
                        attempt=attempt_index,
                        outcome="loading_timeout",
                        polls=polls,
                        foreground_app=last_foreground,
                    )
                )
            if attempt_index < retries:
                await asyncio.sleep(0.5)
        raise MobileUseError(
            ErrorCode.TIMEOUT,
            f"App {package!r} did not reach the foreground after {retries} attempts; "
            f"last foreground package was {last_foreground.package!r}.",
            "Inspect the current screen for a permission dialog or another app, then retry.",
            data={
                "requested_package": package,
                "last_foreground_app": last_foreground.model_dump(mode="json"),
                "attempts": [item.model_dump(mode="json") for item in attempts],
            },
        )

    async def terminate_app(self, package: str) -> None:
        await self._call_blocking(self.adb_device.app_stop, package)

    async def open_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MobileUseError(
                ErrorCode.OPERATION_FAILED,
                "Only absolute http:// and https:// URLs are supported.",
            )
        await self._call_blocking(self.adb_device.open_browser, url)

    async def get_foreground_app(self) -> ForegroundApp:
        try:
            current = await self._call_blocking(self.adb_device.app_current)
        except Exception:
            return ForegroundApp()
        return ForegroundApp(package=current.package or None, activity=current.activity or None)

    async def list_apps(self, query: str | None = None, limit: int = 100) -> list[str]:
        output = await self._call_blocking(
            self.adb_device.shell,
            ["pm", "list", "packages", "-3"],
            timeout=self.config.subprocess_timeout_seconds,
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

    async def start_recording(
        self,
        max_duration_seconds: int,
        bit_rate: int | None = None,
    ) -> dict[str, object]:
        return await self.recording.start(max_duration_seconds, bit_rate)

    async def stop_recording(self) -> dict[str, object]:
        return await self.recording.stop()

    def disconnect(self) -> None:
        self.recording.abort()
        self.android_client.disconnect()

    async def aclose(self) -> None:
        """Await the same recording/process cleanup used by session shutdown."""

        await self.recording.aclose()
        try:
            await run_blocking(
                self.android_client.disconnect,
                timeout_seconds=self.config.subprocess_timeout_seconds,
            )
        except ProcessTimeoutError:
            # ``disconnect`` is best effort, but its owner must never retain a
            # task that can outlive the session indefinitely.
            pass
        finally:
            # adbutils owns a separate transport from uiautomator2.  Closing
            # both adapters on the successful path prevents reconnects and
            # stdio EOF from retaining an idle socket.
            self._close_adapters()
