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
    ResolvedTarget,
    ScreenshotCapture,
    ScreenSnapshot,
    SwipePercentage,
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


def normalized_coordinate_to_pixel(value: float, size: int) -> int:
    """Map a normalized coordinate onto the valid native pixel range."""

    # 1.0 denotes the last valid pixel rather than an out-of-range width.
    return min(size - 1, max(0, round(value * (size - 1))))


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


def _optional_method(instance: Any, name: str) -> Callable[..., Any] | None:
    """Read an optional adapter method without accepting mock auto-attrs."""

    candidate = getattr(instance, name, None)
    if not callable(candidate):
        return None
    owner_type = cast(type[Any], type(instance))
    if (
        getattr(owner_type, name, None) is not None
        or name in getattr(instance, "__dict__", {})
        or hasattr(candidate, "assert_awaited")
    ):
        return candidate
    return None


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
        """Capture a coherent best-effort observation.

        Pixels are the required component.  Accessibility hierarchy and
        foreground-app reads are optional so a transient failure in either
        does not discard an otherwise useful screenshot.  The warning strings
        are stable and deliberately omit raw adapter exception details.
        """

        capture = await self.screenshot()
        warnings: list[str] = []
        elements = []
        try:
            hierarchy_reader = _optional_method(self.android_client, "get_hierarchy")
            if hierarchy_reader is None:
                # Keep compatibility with injected clients predating the
                # hierarchy-free screenshot method.
                legacy = await self._call_blocking(self.android_client.get_screen)
                hierarchy = legacy[1]
            else:
                hierarchy = await self._call_blocking(hierarchy_reader)
            elements = parse_hierarchy(
                hierarchy,
                interactive_only=interactive_only,
                max_elements=max_elements,
                max_text_length=max_text_length,
            )
        except Exception:
            warnings.append("HIERARCHY_UNAVAILABLE: Android UI hierarchy could not be collected.")
        try:
            foreground = await self._read_foreground_app()
        except Exception:
            foreground = ForegroundApp()
            warnings.append(
                "FOREGROUND_APP_UNAVAILABLE: Android foreground app could not be collected."
            )
        return ScreenSnapshot(
            serial=self.serial,
            width=capture.width,
            height=capture.height,
            screenshot_png=capture.screenshot_png,
            elements=elements,
            foreground_app=foreground,
            warnings=warnings,
        )

    async def screenshot(self) -> ScreenshotCapture:
        """Capture only native pixels; never request hierarchy or foreground app."""

        screenshot_reader = _optional_method(self.android_client, "get_screenshot")
        if screenshot_reader is not None:
            result = await self._call_blocking(screenshot_reader)
            if isinstance(result, ScreenshotCapture):
                return result
            if isinstance(result, tuple):
                result_tuple = cast(tuple[object, ...], result)
                if len(result_tuple) != 3:
                    result_tuple = ()
            else:
                result_tuple = ()
            if len(result_tuple) == 3:
                screenshot, width, height = cast(tuple[bytes, int, int], result_tuple)
                return ScreenshotCapture(
                    serial=self.serial,
                    screenshot_png=screenshot,
                    width=width,
                    height=height,
                )
        # Compatibility path for an injected pre-T06 AndroidClient.  The
        # production client always takes the hierarchy-free branch above.
        legacy = await self._call_blocking(self.android_client.get_screen)
        screenshot, _hierarchy, width, height = legacy
        return ScreenshotCapture(
            serial=self.serial,
            screenshot_png=screenshot,
            width=width,
            height=height,
        )

    async def capture_screenshot(self) -> ScreenshotCapture:
        """Explicit alias for callers that prefer a capture verb."""

        return await self.screenshot()

    async def tap(self, target: Target) -> dict[str, object]:
        _, resolved = await self.resolve_target(target)
        x, y = resolved.bounds.center
        await self._call_blocking(self.adb_device.click, x, y)
        return {
            "x": x,
            "y": y,
            "selector": resolved.selector,
            "matched_selector": resolved.matched_selector or resolved.selector,
            "attempts": [attempt.model_dump() for attempt in resolved.attempts],
        }

    async def long_press(self, target: Target, duration_ms: int = 1_000) -> dict[str, object]:
        _, resolved = await self.resolve_target(target)
        x, y = resolved.bounds.center
        await self._call_blocking(self.adb_device.swipe, x, y, x, y, duration_ms / 1_000)
        return {
            "x": x,
            "y": y,
            "duration_ms": duration_ms,
            "selector": resolved.selector,
            "matched_selector": resolved.matched_selector or resolved.selector,
            "attempts": [attempt.model_dump() for attempt in resolved.attempts],
        }

    async def resolve_target(
        self,
        target: Target,
        *,
        observation: ScreenSnapshot | None = None,
    ) -> tuple[ScreenSnapshot, ResolvedTarget]:
        """Read and resolve against the complete current hierarchy.

        Action resolution must not inherit the compact response limit used by
        ``android_snapshot``. The observation is returned so a session can
        validate a capability against the exact hierarchy before dispatch.
        """

        current = observation or await self.snapshot(max_elements=None)
        resolved = resolve_target(
            target,
            current.elements,
            screen_width=current.width,
            screen_height=current.height,
        )
        return current, resolved

    async def tap_resolved(self, resolved: ResolvedTarget) -> dict[str, object]:
        """Dispatch a previously validated tap without re-resolving it."""

        x, y = resolved.bounds.center
        await self._call_blocking(self.adb_device.click, x, y)
        return {
            "x": x,
            "y": y,
            "selector": resolved.selector,
            "matched_selector": resolved.matched_selector or resolved.selector,
            "attempts": [attempt.model_dump() for attempt in resolved.attempts],
        }

    async def long_press_resolved(
        self,
        resolved: ResolvedTarget,
        duration_ms: int = 1_000,
    ) -> dict[str, object]:
        """Dispatch a previously validated long press without re-resolving it."""

        x, y = resolved.bounds.center
        await self._call_blocking(self.adb_device.swipe, x, y, x, y, duration_ms / 1_000)
        return {
            "x": x,
            "y": y,
            "duration_ms": duration_ms,
            "selector": resolved.selector,
            "matched_selector": resolved.matched_selector or resolved.selector,
            "attempts": [attempt.model_dump() for attempt in resolved.attempts],
        }

    async def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 400,
    ) -> dict[str, object]:
        # Coordinate validation only needs dimensions, but the complete
        # screenshot/hierarchy observation keeps the session's current screen
        # check coherent and never imposes the compact element limit.
        snapshot = await self.snapshot(interactive_only=True, max_elements=None)
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
        return {
            "start": {"x": start_x, "y": start_y},
            "end": {"x": end_x, "y": end_y},
            "duration_ms": duration_ms,
            "screen_width": snapshot.width,
            "screen_height": snapshot.height,
        }

    @staticmethod
    def _percentage_pixels(value: float, size: int) -> int:
        return normalized_coordinate_to_pixel(value, size)

    async def swipe_percentage(
        self, coordinates: SwipePercentage, duration_ms: int = 400
    ) -> dict[str, object]:
        """Convert normalized coordinates using the current native dimensions."""

        snapshot = await self.snapshot(interactive_only=True, max_elements=None)
        start_x = self._percentage_pixels(coordinates.start_x, snapshot.width)
        start_y = self._percentage_pixels(coordinates.start_y, snapshot.height)
        end_x = self._percentage_pixels(coordinates.end_x, snapshot.width)
        end_y = self._percentage_pixels(coordinates.end_y, snapshot.height)
        await self._call_blocking(
            self.adb_device.swipe,
            start_x,
            start_y,
            end_x,
            end_y,
            duration_ms / 1_000,
        )
        return {
            "start": {"x": start_x, "y": start_y},
            "end": {"x": end_x, "y": end_y},
            "duration_ms": duration_ms,
            "screen_width": snapshot.width,
            "screen_height": snapshot.height,
            "coordinates": coordinates.model_dump(mode="json"),
        }

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
            snapshot = await self.snapshot(max_elements=None)
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
            return await self._read_foreground_app()
        except Exception:
            # A standalone foreground query preserves its historical safe
            # empty result.  Combined snapshots call the raising helper so
            # they can expose a component warning instead.
            return ForegroundApp()

    async def _read_foreground_app(self) -> ForegroundApp:
        """Read foreground state and preserve adapter failures for callers."""

        current = await self._call_blocking(self.adb_device.app_current)
        if current is None:
            raise RuntimeError("foreground app state was unavailable")
        package = getattr(current, "package", None)
        activity = getattr(current, "activity", None)
        if package is not None and not isinstance(package, str):
            raise RuntimeError("invalid foreground package")
        if activity is not None and not isinstance(activity, str):
            raise RuntimeError("invalid foreground activity")
        return ForegroundApp(package=package or None, activity=activity or None)

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
