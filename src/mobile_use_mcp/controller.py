"""Deterministic Android operations used by MCP tool handlers."""

import asyncio
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol, cast
from urllib.parse import urlparse

from adbutils import AdbClient  # pyright: ignore[reportMissingTypeStubs]

from mobile_use_mcp.android_client import AndroidClient, InputCommandError
from mobile_use_mcp.config import RuntimeConfig
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import (
    AppInfo,
    AppInventory,
    AppLaunchAttempt,
    AppLaunchResult,
    AppTerminationResult,
    ForegroundApp,
    ResolvedTarget,
    ScreenshotCapture,
    ScreenSnapshot,
    SwipePercentage,
    Target,
    UIElement,
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


@dataclass(frozen=True, slots=True)
class InputOutcome:
    """Safe, stage-separated result for text input and clearing.

    The object intentionally contains no input value.  ``__eq__`` retains the
    old controller contract where callers compared the returned method with a
    string, while ``to_data`` exposes the richer status to MCP adapters.
    """

    method: str
    command_status: str
    effect_status: str
    restoration_status: str
    focus_status: str
    verification_available: bool
    requires_observation: bool
    fallback_used: bool = False
    attempts: int = 1
    max_attempts: int | None = None

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.method == other
        if not isinstance(other, InputOutcome):
            return NotImplemented
        return (
            self.method,
            self.command_status,
            self.effect_status,
            self.restoration_status,
            self.focus_status,
            self.verification_available,
            self.requires_observation,
            self.fallback_used,
            self.attempts,
            self.max_attempts,
        ) == (
            other.method,
            other.command_status,
            other.effect_status,
            other.restoration_status,
            other.focus_status,
            other.verification_available,
            other.requires_observation,
            other.fallback_used,
            other.attempts,
            other.max_attempts,
        )

    def __str__(self) -> str:
        return self.method

    @property
    def effect_verified(self) -> bool:
        """Whether the observed field state proves the requested effect."""

        return self.effect_status == "verified"

    def to_data(self) -> dict[str, object]:
        """Return an MCP-safe mapping with compatibility aliases."""

        return {
            "method": self.method,
            "command_status": self.command_status,
            "effect_status": self.effect_status,
            "effect_verified": self.effect_verified,
            "restoration_status": self.restoration_status,
            "input_method_status": self.restoration_status,
            "input_method_restoration": self.restoration_status,
            "focus_status": self.focus_status,
            "verification_available": self.verification_available,
            "requires_observation": self.requires_observation,
            "fallback_used": self.fallback_used,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "stages": {
                "focus": {
                    "status": self.focus_status,
                    "verification_available": self.verification_available,
                },
                "execution": {
                    "status": self.command_status,
                    "method": self.method,
                    "fallback_used": self.fallback_used,
                },
                "effect": {
                    "status": self.effect_status,
                    "verified": self.effect_verified,
                    "verification_available": self.verification_available,
                },
                "input_method_restoration": {"status": self.restoration_status},
            },
        }

    def __getitem__(self, key: str) -> object:
        return self.to_data()[key]


TextInputOutcome = InputOutcome
ClearTextOutcome = InputOutcome


@dataclass(frozen=True, slots=True)
class _InputFieldState:
    """In-memory focused-field observation; never serialized or logged."""

    element: UIElement | None = field(repr=False)
    available: bool
    focused: bool
    password_like: bool
    text: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _CommandState:
    method: str
    status: str
    fallback_used: bool = False
    attempts: int = 1


@dataclass(frozen=True, slots=True)
class _ParsedAppMetadata:
    """Metadata extracted from one bounded Android package response."""

    launcher_label: str | None = None
    launchable_activity: str | None = None
    is_system: bool | None = None
    enabled: bool | None = None


def _parse_package_names(output: str) -> list[str]:
    """Parse package names from ``pm list packages`` output."""

    packages: set[str] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith("package:"):
            continue
        package = line.removeprefix("package:").strip()
        # Package names are intentionally kept conservative.  This rejects a
        # malformed shell response without ever treating it as a selector or
        # executable argument.
        if package and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.]*", package):
            packages.add(package)
    return sorted(packages, key=lambda value: (value.casefold(), value))


def _parse_label(output: str) -> str | None:
    """Read a localized/non-localized label when dumpsys exposes one."""

    for pattern in (
        r"\bnonLocalizedLabel=(?:\"([^\"]*)\"|(.+?))(?=\s+[A-Za-z][A-Za-z0-9_]*=|\s*$)",
        r"\blabel=(?:\"([^\"]*)\"|(.+?))(?=\s+[A-Za-z][A-Za-z0-9_]*=|\s*$)",
    ):
        match = re.search(pattern, output)
        if match:
            value = next((group.strip() for group in match.groups() if group is not None), "")
            if value and value.casefold() not in {"null", "none"} and not value.startswith("0x"):
                return value
    return None


def _parse_enabled(output: str) -> bool | None:
    """Parse the package/user enabled state across Android dump formats."""

    match = re.search(r"\benabled\s*=\s*(true|false|0|1)\b", output, re.IGNORECASE)
    if match:
        value = match.group(1).casefold()
        return value in {"true", "1"}
    return None


def _parse_system_flag(output: str) -> bool | None:
    """Infer system classification only from explicit Android package flags."""

    flag_pattern = r"(?:pkgFlags|flags)\s*=\s*(?:\[([^\]]*)\]|(0x[0-9a-f]+))"
    for flag_match in re.finditer(flag_pattern, output, re.I):
        names = (flag_match.group(1) or "").casefold()
        hex_value = flag_match.group(2)
        if "system" in names or "flag_system" in names:
            return True
        if hex_value is not None:
            try:
                if int(hex_value, 16) & 0x1:
                    return True
            except ValueError:
                continue
    explicit = re.search(r"\bisSystem(?:App)?\s*=\s*(true|false)\b", output, re.I)
    if explicit is not None:
        return explicit.group(1).casefold() == "true"
    if re.search(flag_pattern, output, re.I):
        # An explicit flags value without SYSTEM does not prove that the app
        # is third-party; OEMs add unrelated flags and omit some names.
        return (
            False
            if any(
                match.group(2) is not None and int(match.group(2), 16) == 0
                for match in re.finditer(flag_pattern, output, re.I)
            )
            else None
        )
    return None


def _parse_activity(output: str, package: str) -> str | None:
    """Parse a launcher component from resolve/query-activities output."""

    component_pattern = re.compile(rf"(?<![A-Za-z0-9_.]){re.escape(package)}/([^\s,}}]+)")
    match = component_pattern.search(output)
    if match:
        activity = match.group(1).strip()
        if activity and activity.casefold() not in {"null", "none"}:
            return activity
    info_match = re.search(rf"packageName={re.escape(package)}\b.*?\bname=([^\s}}]+)", output, re.S)
    if info_match:
        return info_match.group(1)
    return None


def _parse_app_metadata(output: str, package: str) -> _ParsedAppMetadata:
    return _ParsedAppMetadata(
        launcher_label=_parse_label(output),
        is_system=_parse_system_flag(output),
        enabled=_parse_enabled(output),
        launchable_activity=_parse_activity(output, package),
    )


def _is_shell_error_output(output: str) -> bool:
    """Recognize common ADB/Android shell failures when exit codes are hidden."""

    return (
        re.search(
            r"(?im)^\s*(?:error(?::|\s)|adb:\s|cmd:\s*failure\b|"
            r"exception\b|unknown command\b|unknown option\b|permission denied\b)",
            output,
        )
        is not None
    )


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
            ErrorCode.INPUT_FOCUS_FAILED,
            "The requested Android input target did not become focused.",
            "Call android_snapshot, verify the input element, and try a different selector.",
            data={
                "stage": "focus",
                "focus_status": "unverified",
                "verification_available": True,
            },
        )

    @staticmethod
    def _same_input_element(expected: UIElement, candidate: UIElement) -> bool:
        """Match a focused element without comparing potentially masked text."""

        if expected.resource_id and candidate.resource_id:
            if expected.resource_id.casefold() != candidate.resource_id.casefold():
                return False
            if expected.package and candidate.package:
                return expected.package.casefold() == candidate.package.casefold()
            return True
        if expected.bounds is not None and candidate.bounds == expected.bounds:
            if expected.class_name and candidate.class_name:
                return expected.class_name.casefold() == candidate.class_name.casefold()
            return True
        if expected.content_description and candidate.content_description:
            return expected.content_description.casefold() == (
                candidate.content_description.casefold()
            )
        if expected.text and candidate.text:
            return expected.text == candidate.text
        return False

    async def _capture_focused_field(self) -> _InputFieldState:
        """Read one focused field while keeping its text inside this process."""

        try:
            hierarchy_reader = _optional_method(self.android_client, "get_hierarchy")
            if hierarchy_reader is not None:
                hierarchy = await self._call_blocking(hierarchy_reader)
                elements = parse_hierarchy(
                    hierarchy,
                    interactive_only=False,
                    max_elements=None,
                    max_text_length=min(2_000, self.config.snapshot_max_text_length),
                )
                unavailable = False
            else:
                # Compatibility path for injected clients predating the
                # hierarchy-free AndroidClient component.
                snapshot = await self.snapshot(
                    interactive_only=False,
                    max_elements=None,
                    max_text_length=min(2_000, self.config.snapshot_max_text_length),
                )
                elements = snapshot.elements
                unavailable = any(
                    warning.startswith("HIERARCHY_UNAVAILABLE") for warning in snapshot.warnings
                )
        except ProcessTimeoutError:
            raise
        except Exception:
            return _InputFieldState(
                element=None,
                available=False,
                focused=False,
                password_like=False,
            )
        if unavailable:
            return _InputFieldState(
                element=None,
                available=False,
                focused=False,
                password_like=False,
            )
        focused = [element for element in elements if element.focused]
        if len(focused) != 1:
            return _InputFieldState(
                element=None,
                available=True,
                focused=False,
                password_like=False,
            )
        element = focused[0]
        return _InputFieldState(
            element=element,
            available=True,
            focused=True,
            password_like=element.is_password_like,
            text=element.text,
        )

    async def verify_focused_element(
        self,
        expected: UIElement | None = None,
        *,
        timeout_seconds: float = 2,
    ) -> _InputFieldState:
        """Re-read accessibility focus immediately before text execution."""

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            state = await self._capture_focused_field()
            if (
                state.focused
                and state.element is not None
                and (expected is None or self._same_input_element(expected, state.element))
            ):
                return state
            await asyncio.sleep(0.1)
        raise MobileUseError(
            ErrorCode.INPUT_FOCUS_FAILED,
            "The Android input field was not focused at execution time.",
            "Call android_snapshot, verify the input element, and try again.",
            data={
                "stage": "focus",
                "focus_status": "unverified",
                "verification_available": True,
            },
            retryable_override=False,
        )

    async def _dispatch_adb_text(self, text: str) -> _CommandState:
        """Use the parameterized ADB fallback only after a definite failure."""

        shell = getattr(self.adb_device, "shell", None)
        if not callable(shell):
            return _CommandState(method="adb", status="failed", fallback_used=True)
        try:
            await self._call_blocking(
                shell,
                ["input", "text", text],
                timeout=self.config.subprocess_timeout_seconds,
            )
        except ProcessTimeoutError:
            raise
        except Exception:
            # A shell transport can fail after Android accepted the event.
            # Never make a caller retry this payload blindly.
            return _CommandState(method="adb", status="uncertain", fallback_used=True)
        return _CommandState(method="adb", status="sent", fallback_used=True)

    async def _dispatch_adb_clear(self, characters: int) -> _CommandState:
        """Send a bounded number of delete key events through ADB."""

        keyevent = getattr(self.adb_device, "keyevent", None)
        if not callable(keyevent):
            return _CommandState(
                method="adb",
                status="failed",
                fallback_used=True,
                attempts=0,
            )
        attempts = 0
        try:
            for _ in range(characters):
                await self._call_blocking(keyevent, "DEL")
                attempts += 1
        except ProcessTimeoutError:
            raise
        except Exception:
            return _CommandState(
                method="adb",
                status="uncertain",
                fallback_used=True,
                attempts=attempts,
            )
        return _CommandState(
            method="adb",
            status="sent",
            fallback_used=True,
            attempts=characters,
        )

    async def _dispatch_text_command(self, text: str) -> _CommandState:
        command = _optional_method(self.android_client, "send_text_command")
        if command is None:
            # Older injected clients expose only the compatibility wrapper.
            # That wrapper performs its own cleanup, so a failure is still
            # treated as uncertain and is never sent again blindly.
            command = _optional_method(self.android_client, "send_text")
            if command is None:
                return await self._dispatch_adb_text(text)
            try:
                await self._call_blocking(command, text)
            except ProcessTimeoutError:
                raise
            except Exception:
                return _CommandState(method="uiautomator2", status="uncertain")
            return _CommandState(method="uiautomator2", status="sent")
        try:
            await self._call_blocking(command, text)
        except ProcessTimeoutError:
            raise
        except InputCommandError as error:
            if not error.may_have_succeeded:
                return await self._dispatch_adb_text(text)
            return _CommandState(method="uiautomator2", status="uncertain")
        except AttributeError:
            # The method was present when selected; an AttributeError from
            # inside an injected adapter can occur after dispatch and is
            # therefore still an uncertain outcome.
            return _CommandState(method="uiautomator2", status="uncertain")
        except Exception:
            return _CommandState(method="uiautomator2", status="uncertain")
        return _CommandState(method="uiautomator2", status="sent")

    async def _dispatch_clear_command(self, characters: int) -> _CommandState:
        command = _optional_method(self.android_client, "clear_text_command")
        if command is None:
            command = _optional_method(self.android_client, "clear_text")
            if command is None:
                return await self._dispatch_adb_clear(characters)
            try:
                await self._call_blocking(command)
            except ProcessTimeoutError:
                raise
            except Exception:
                return _CommandState(method="uiautomator2", status="uncertain")
            return _CommandState(method="uiautomator2", status="sent")
        try:
            await self._call_blocking(command)
        except ProcessTimeoutError:
            raise
        except InputCommandError as error:
            if not error.may_have_succeeded:
                return await self._dispatch_adb_clear(characters)
            return _CommandState(method="uiautomator2", status="uncertain")
        except AttributeError:
            return _CommandState(method="uiautomator2", status="uncertain")
        except Exception:
            return _CommandState(method="uiautomator2", status="uncertain")
        return _CommandState(method="uiautomator2", status="sent")

    async def _restore_input_method(self, *, compatibility_wrapper: bool = False) -> str:
        if compatibility_wrapper:
            return "not_required"
        restore = _optional_method(self.android_client, "restore_input_method")
        if restore is None:
            return "unavailable"
        try:
            await self._call_blocking(restore)
        except ProcessTimeoutError:
            raise
        except Exception:
            return "failed"
        return "restored"

    @staticmethod
    def _verify_text_effect(
        before: _InputFieldState,
        after: _InputFieldState,
        text: str,
    ) -> tuple[str, bool]:
        if not before.available or not after.available:
            return "unverified", False
        if before.element is None or after.element is None or not after.focused:
            return "unverified", False
        if not AndroidController._same_input_element(before.element, after.element):
            return "unverified", False
        if before.password_like or after.password_like:
            return "unverified", False
        if before.text is None or after.text is None:
            return "unverified", False
        expected = before.text + text
        return ("verified", True) if after.text == expected else ("not_applied", True)

    @staticmethod
    def _text_effect_is_known_unchanged(
        before: _InputFieldState,
        after: _InputFieldState,
    ) -> bool:
        """Return true only when accessibility proves no input was applied."""

        return (
            before.available
            and after.available
            and before.element is not None
            and after.element is not None
            and AndroidController._same_input_element(before.element, after.element)
            and before.focused
            and after.focused
            and not before.password_like
            and not after.password_like
            and before.text is not None
            and before.text == after.text
        )

    @staticmethod
    def _verify_clear_effect(
        before: _InputFieldState,
        after: _InputFieldState,
    ) -> tuple[str, bool]:
        if (
            not before.available
            or not after.available
            or before.element is None
            or after.element is None
            or not before.focused
            or not after.focused
            or not AndroidController._same_input_element(before.element, after.element)
        ):
            return "unverified", False
        if before.password_like or after.password_like or before.text is None or after.text is None:
            return "unverified", False
        return ("verified", True) if after.text == "" else ("not_empty", True)

    @staticmethod
    def _input_error(
        code: ErrorCode,
        message: str,
        suggestion: str,
        outcome: InputOutcome,
        *,
        stage: str,
    ) -> MobileUseError:
        details = outcome.to_data()
        details["stage"] = stage
        return MobileUseError(
            code,
            message,
            suggestion,
            data=details,
            retryable_override=False,
        )

    async def type_text_detailed(
        self,
        text: str,
        target: Target | None = None,
        *,
        expected_element: UIElement | None = None,
        focus_status: str | None = None,
    ) -> InputOutcome:
        """Type text with independent focus, execution, effect, and cleanup stages."""

        compatibility_wrapper = _optional_method(self.android_client, "send_text_command") is None
        if target is not None:
            await self.focus(target)
            focus_status = "verified"
        before = await self._capture_focused_field()
        if expected_element is not None and (
            not before.focused
            or before.element is None
            or not self._same_input_element(expected_element, before.element)
        ):
            raise MobileUseError(
                ErrorCode.INPUT_FOCUS_FAILED,
                "The Android input field changed before text execution.",
                "Call android_snapshot, verify the input element, and try again.",
                data={
                    "stage": "focus",
                    "focus_status": "unverified",
                    "verification_available": before.available,
                },
                retryable_override=False,
            )
        if expected_element is not None:
            focus_status = focus_status or "verified"
        if focus_status is None:
            focus_status = "current" if before.focused else "unverified"
        command = await self._dispatch_text_command(text)
        restoration = await self._restore_input_method(
            compatibility_wrapper=compatibility_wrapper or command.method == "adb",
        )
        after = await self._capture_focused_field()
        effect_status, verification_available = self._verify_text_effect(before, after, text)

        # An adapter exception is conservatively uncertain. If accessibility
        # proves that the field stayed byte-for-byte unchanged, a single ADB
        # fallback is safe; otherwise the original command is never repeated.
        if (
            command.status == "uncertain"
            and restoration == "restored"
            and self._text_effect_is_known_unchanged(before, after)
        ):
            command = await self._dispatch_adb_text(text)
            restoration = await self._restore_input_method(compatibility_wrapper=True)
            after = await self._capture_focused_field()
            effect_status, verification_available = self._verify_text_effect(before, after, text)

        outcome = InputOutcome(
            method=command.method,
            command_status=command.status,
            effect_status=effect_status,
            restoration_status=restoration,
            focus_status=focus_status,
            verification_available=verification_available,
            requires_observation=effect_status != "verified",
            fallback_used=command.fallback_used,
            attempts=command.attempts,
        )
        if restoration in {"failed", "unavailable"} and command.method == "uiautomator2":
            raise self._input_error(
                ErrorCode.INPUT_METHOD_RESTORE_FAILED,
                "Android text input completed, but the temporary input method could not be "
                "restored.",
                "Do not resend the text. Inspect the device input method and observe the field.",
                outcome,
                stage="input_method_restoration",
            )
        if command.status == "failed":
            raise self._input_error(
                ErrorCode.INPUT_EXECUTION_FAILED,
                "Android text input could not be dispatched.",
                "Call android_snapshot and retry only after confirming that no text was entered.",
                outcome,
                stage="execution",
            )
        if command.status == "uncertain":
            raise self._input_error(
                ErrorCode.INPUT_EXECUTION_UNCERTAIN,
                "Android text input may have been dispatched, but its command result is uncertain.",
                "Do not resend the text; observe the field before deciding whether another "
                "action is needed.",
                outcome,
                stage="execution",
            )
        return outcome

    async def type_text(self, text: str, target: Target | None = None) -> InputOutcome:
        """Compatibility entry point for stage-separated text input."""

        return await self.type_text_detailed(text, target)

    async def clear_text_detailed(
        self,
        characters: int = 100,
        target: Target | None = None,
        *,
        expected_element: UIElement | None = None,
        focus_status: str | None = None,
    ) -> InputOutcome:
        """Clear text with a bounded fallback and an explicit effect check."""

        bounded_characters = max(1, min(characters, 2_000))
        compatibility_wrapper = _optional_method(self.android_client, "clear_text_command") is None
        if target is not None:
            await self.focus(target)
            focus_status = "verified"
        before = await self._capture_focused_field()
        if expected_element is not None and (
            not before.focused
            or before.element is None
            or not self._same_input_element(expected_element, before.element)
        ):
            raise MobileUseError(
                ErrorCode.INPUT_FOCUS_FAILED,
                "The Android input field changed before text clearing.",
                "Call android_snapshot, verify the input element, and try again.",
                data={
                    "stage": "focus",
                    "focus_status": "unverified",
                    "verification_available": before.available,
                },
                retryable_override=False,
            )
        if expected_element is not None:
            focus_status = focus_status or "verified"
        if focus_status is None:
            focus_status = "current" if before.focused else "unverified"
        command = await self._dispatch_clear_command(bounded_characters)
        restoration = await self._restore_input_method(
            compatibility_wrapper=compatibility_wrapper or command.method == "adb",
        )
        after = await self._capture_focused_field()
        effect_status, verification_available = self._verify_clear_effect(before, after)

        # A successful uiautomator2 command that is observably incomplete can
        # safely be followed by bounded delete presses. This path never runs
        # when the first command or its cleanup is uncertain.
        if (
            command.status == "sent"
            and command.method == "uiautomator2"
            and effect_status == "not_empty"
            and restoration == "restored"
        ):
            command = await self._dispatch_adb_clear(bounded_characters)
            restoration = await self._restore_input_method(compatibility_wrapper=True)
            after = await self._capture_focused_field()
            effect_status, verification_available = self._verify_clear_effect(before, after)

        if (
            command.status == "uncertain"
            and restoration == "restored"
            and effect_status == "not_empty"
        ):
            command = await self._dispatch_adb_clear(bounded_characters)
            restoration = await self._restore_input_method(compatibility_wrapper=True)
            after = await self._capture_focused_field()
            effect_status, verification_available = self._verify_clear_effect(before, after)

        outcome = InputOutcome(
            method=command.method,
            command_status=command.status,
            effect_status=effect_status,
            restoration_status=restoration,
            focus_status=focus_status,
            verification_available=verification_available,
            requires_observation=effect_status != "verified",
            fallback_used=command.fallback_used,
            attempts=command.attempts,
            max_attempts=bounded_characters,
        )
        if restoration in {"failed", "unavailable"} and command.method == "uiautomator2":
            raise self._input_error(
                ErrorCode.INPUT_METHOD_RESTORE_FAILED,
                "Android text clearing completed, but the temporary input method could not be "
                "restored.",
                "Do not repeat the clear command blindly. Inspect the field and input method "
                "first.",
                outcome,
                stage="input_method_restoration",
            )
        if command.status == "failed":
            raise self._input_error(
                ErrorCode.CLEAR_EXECUTION_FAILED,
                "Android text clearing could not be dispatched.",
                "Call android_snapshot and retry only after confirming the focused field.",
                outcome,
                stage="execution",
            )
        if command.status == "uncertain":
            raise self._input_error(
                ErrorCode.CLEAR_EXECUTION_UNCERTAIN,
                "Android text clearing may have been dispatched, but its command result is "
                "uncertain.",
                "Observe the field before deciding whether another clear action is needed.",
                outcome,
                stage="execution",
            )
        return outcome

    async def clear_text(self, characters: int = 100, target: Target | None = None) -> InputOutcome:
        """Compatibility entry point for bounded, stage-separated text clearing."""

        return await self.clear_text_detailed(characters, target)

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
        activity: str | None = None,
        retries: int = 3,
        timeout_seconds: float = 15,
        stabilization_polls: int = 2,
    ) -> AppLaunchResult:
        """Launch one package and verify a stable foreground transition.

        ``app_start`` only proves that a command was accepted by ADB.  This
        method keeps polling the foreground app until the requested package is
        observed for a short consecutive window.  A different foreground
        package is recorded as a blocker, while a failed foreground read is a
        separate outcome and is never represented as an empty/loading state.
        """

        if not package.strip():
            raise MobileUseError(
                ErrorCode.APP_LAUNCH_FAILED,
                "An Android package name is required to launch an app.",
                "Call android_list_apps and pass an exact package name.",
                retryable_override=False,
            )
        bounded_retries = max(1, min(retries, 10))
        bounded_timeout = max(0.01, min(timeout_seconds, 300.0))
        bounded_stabilization = max(1, min(stabilization_polls, 5))
        attempts: list[AppLaunchAttempt] = []
        last_foreground = ForegroundApp()
        blockers: list[ForegroundApp] = []
        foreground_error: MobileUseError | None = None
        for attempt_index in range(1, bounded_retries + 1):
            transitions: list[ForegroundApp] = []
            blocker: ForegroundApp | None = None
            candidate_polls = 0
            polls = 0
            try:
                if activity is None:
                    await self._call_blocking(self.adb_device.app_start, package)
                else:
                    await self._call_blocking(self.adb_device.app_start, package, activity)
            except Exception as error:
                attempts.append(
                    AppLaunchAttempt(
                        attempt=attempt_index,
                        outcome="start_failed",
                        polls=0,
                        foreground_app=last_foreground,
                        command_status="failed",
                        effect_status="unverified",
                        error_code=ErrorCode.APP_LAUNCH_FAILED.value,
                    )
                )
                raise MobileUseError(
                    ErrorCode.APP_LAUNCH_FAILED,
                    f"Android could not start package {package!r}.",
                    "Check that the exact package is installed with android_list_apps.",
                    data={
                        "stage": "start_command",
                        "attempt": attempt_index,
                        "requested_package": package,
                        "command_status": "failed",
                        "effect_status": "unverified",
                        "attempts": [item.model_dump(mode="json") for item in attempts],
                    },
                    retryable_override=False,
                ) from error
            deadline = asyncio.get_running_loop().time() + bounded_timeout
            while asyncio.get_running_loop().time() < deadline:
                polls += 1
                try:
                    last_foreground = await self._read_foreground_app()
                except MobileUseError as error:
                    foreground_error = error
                    attempts.append(
                        AppLaunchAttempt(
                            attempt=attempt_index,
                            outcome="foreground_unavailable",
                            polls=polls,
                            foreground_app=last_foreground,
                            command_status="sent",
                            effect_status="unverified",
                            error_code=error.code.value,
                            transitions=transitions,
                        )
                    )
                    # Repeating app_start after a foreground read failure is
                    # not useful and could hide an accepted launch command.
                    break
                if len(transitions) < 50:
                    transitions.append(last_foreground)
                else:
                    # Keep the first transition history and the latest safe
                    # state without allowing long stabilization windows to
                    # exceed the typed response budget.
                    transitions[-1] = last_foreground
                if last_foreground.package == package:
                    candidate_polls += 1
                    if candidate_polls >= bounded_stabilization:
                        attempts.append(
                            AppLaunchAttempt(
                                attempt=attempt_index,
                                outcome="ready",
                                polls=polls,
                                foreground_app=last_foreground,
                                command_status="sent",
                                effect_status="verified",
                                blocker=blocker,
                                transitions=transitions,
                            )
                        )
                        return AppLaunchResult(
                            requested_package=package,
                            launchable_activity=activity,
                            command_status="sent",
                            effect_status="verified",
                            verification_available=True,
                            stabilized=True,
                            blockers=blockers,
                            foreground_app=last_foreground,
                            attempts=attempts,
                        )
                else:
                    candidate_polls = 0
                    if last_foreground.package is not None:
                        blocker = last_foreground
                        if len(blockers) < 50 and all(
                            existing.package != last_foreground.package
                            or existing.activity != last_foreground.activity
                            for existing in blockers
                        ):
                            blockers.append(last_foreground)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining > 0 and last_foreground.package != package:
                    await asyncio.sleep(min(0.1, remaining))

            if attempts and attempts[-1].attempt == attempt_index:
                # A foreground read failure already produced the complete
                # attempt record above.
                pass
            elif blocker is not None:
                attempts.append(
                    AppLaunchAttempt(
                        attempt=attempt_index,
                        outcome="blocked_by_other_app",
                        polls=polls,
                        foreground_app=last_foreground,
                        command_status="sent",
                        effect_status="blocked",
                        blocker=blocker,
                        transitions=transitions,
                    )
                )
            else:
                outcome = "foreground_empty" if not last_foreground.package else "loading_timeout"
                attempts.append(
                    AppLaunchAttempt(
                        attempt=attempt_index,
                        outcome=outcome,
                        polls=polls,
                        foreground_app=last_foreground,
                        command_status="sent",
                        effect_status="unverified",
                        transitions=transitions,
                    )
                )
            if foreground_error is not None:
                # A foreground read failure makes the launch postcondition
                # unknowable.  Do not send another potentially duplicate
                # start command while the device state is unobservable.
                break
            if attempt_index < bounded_retries and foreground_error is None:
                await asyncio.sleep(0.1)
        if foreground_error is not None:
            data = {
                "requested_package": package,
                "command_status": "sent",
                "effect_status": "unverified",
                "verification_available": False,
                "foreground_state": "unavailable",
                "last_foreground_app": last_foreground.model_dump(mode="json"),
                "attempts": [item.model_dump(mode="json") for item in attempts],
                "blockers": [item.model_dump(mode="json") for item in blockers],
            }
            raise MobileUseError(
                ErrorCode.FOREGROUND_UNAVAILABLE,
                f"Android could not verify the foreground while launching {package!r}.",
                "Resolve the foreground read failure and inspect the screen before retrying.",
                data=data,
            ) from foreground_error
        raise MobileUseError(
            ErrorCode.TIMEOUT,
            f"App {package!r} did not reach the foreground after {bounded_retries} attempts; "
            f"last foreground package was {last_foreground.package!r}.",
            "Inspect the current screen for a permission dialog or another app, then retry.",
            data={
                "requested_package": package,
                "command_status": "sent",
                "effect_status": "blocked" if blockers else "unverified",
                "verification_available": True,
                "foreground_state": "occupied" if blockers else "empty",
                "last_foreground_app": last_foreground.model_dump(mode="json"),
                "attempts": [item.model_dump(mode="json") for item in attempts],
                "blockers": [item.model_dump(mode="json") for item in blockers],
            },
        )

    async def terminate_app(self, package: str) -> AppTerminationResult:
        """Force-stop one exact package and report the observable effect."""

        if not package.strip():
            raise MobileUseError(
                ErrorCode.APP_TERMINATION_FAILED,
                "An Android package name is required to terminate an app.",
                "Call android_list_apps and pass an exact package name.",
                retryable_override=False,
            )
        foreground_before: ForegroundApp | None = None
        with suppress(MobileUseError):
            foreground_before = await self._read_foreground_app()
            # A pre-command read is useful evidence but must not prevent the
            # requested force-stop.  The postcondition remains explicitly
            # unverified when the subsequent read also fails.
        try:
            await self._call_blocking(self.adb_device.app_stop, package)
        except Exception as error:
            raise MobileUseError(
                ErrorCode.APP_TERMINATION_FAILED,
                f"Android could not terminate package {package!r}.",
                "Check that the exact package is installed and the device is responsive.",
                data={
                    "requested_package": package,
                    "targeted_package": package,
                    "command_status": "failed",
                    "effect_status": "unverified",
                },
                retryable_override=False,
            ) from error
        foreground_after: ForegroundApp | None = None
        verification_reason: str | None = None
        verification_available = True
        try:
            foreground_after = await self._read_foreground_app()
        except MobileUseError as error:
            verification_available = False
            verification_reason = error.code.value
        effect_verified = bool(
            verification_available
            and foreground_before is not None
            and foreground_before.package == package
            and foreground_after is not None
            and foreground_after.package != package
        )
        if not effect_verified and verification_reason is None:
            verification_reason = (
                "target_package_remains_foreground"
                if foreground_after is not None and foreground_after.package == package
                else "target_package_was_not_foreground"
                if foreground_before is None or foreground_before.package != package
                else "foreground_changed_without_process_proof"
            )
        return AppTerminationResult(
            requested_package=package,
            targeted_package=package,
            command_status="sent",
            effect_status="verified" if effect_verified else "unverified",
            effect_verified=effect_verified,
            verification_available=verification_available,
            requires_observation=not effect_verified,
            foreground_before=foreground_before,
            foreground_after=foreground_after,
            verification_reason=verification_reason,
        )

    async def open_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MobileUseError(
                ErrorCode.OPERATION_FAILED,
                "Only absolute http:// and https:// URLs are supported.",
            )
        await self._call_blocking(self.adb_device.open_browser, url)

    async def get_foreground_app(self) -> ForegroundApp:
        """Return the current foreground app or a typed read failure.

        An empty ``ForegroundApp`` is a real Android state (for example, a
        transient launcher/transition surface).  It must not be used as a
        fallback for an adapter failure because launch verification relies on
        distinguishing those two cases.
        """

        return await self._read_foreground_app()

    async def _read_foreground_app(self) -> ForegroundApp:
        """Read foreground state and preserve adapter failures for callers."""

        try:
            current = await self._call_blocking(self.adb_device.app_current)
        except Exception as error:
            raise MobileUseError(
                ErrorCode.FOREGROUND_UNAVAILABLE,
                "Android foreground app state could not be read.",
                "Verify that the device is connected and responsive, then retry.",
                data={"stage": "foreground_read", "foreground_state": "unavailable"},
            ) from error
        if current is None:
            raise MobileUseError(
                ErrorCode.FOREGROUND_UNAVAILABLE,
                "Android foreground app state could not be read.",
                "Verify that the device is connected and responsive, then retry.",
                data={"stage": "foreground_read", "foreground_state": "unavailable"},
            )
        package = getattr(current, "package", None)
        activity = getattr(current, "activity", None)
        if package is not None and not isinstance(package, str):
            raise MobileUseError(
                ErrorCode.FOREGROUND_UNAVAILABLE,
                "Android returned invalid foreground app metadata.",
                "Retry the foreground read after checking the device state.",
                data={"stage": "foreground_read", "foreground_state": "unavailable"},
            )
        if activity is not None and not isinstance(activity, str):
            raise MobileUseError(
                ErrorCode.FOREGROUND_UNAVAILABLE,
                "Android returned invalid foreground activity metadata.",
                "Retry the foreground read after checking the device state.",
                data={"stage": "foreground_read", "foreground_state": "unavailable"},
            )
        return ForegroundApp(package=package or None, activity=activity or None)

    async def list_apps(self, query: str | None = None, limit: int = 100) -> list[str]:
        """Return package strings for compatibility with the original core API."""

        inventory = await self.list_app_inventory(query=query, include_system=False)
        return [app.package for app in inventory.apps[:limit]]

    async def _app_shell(self, arguments: list[str]) -> str:
        """Run one bounded, parameterized ADB query for app metadata."""

        try:
            output = await self._call_blocking(
                self.adb_device.shell,
                arguments,
                timeout=self.config.subprocess_timeout_seconds,
            )
        except MobileUseError:
            raise
        except ProcessTimeoutError as error:
            raise MobileUseError(
                ErrorCode.TIMEOUT,
                "Timed out while querying Android app metadata.",
                "Check the device connection and retry the bounded app query.",
                data={"stage": "app_query", "arguments": arguments[:4]},
            ) from error
        except Exception as error:
            raise MobileUseError(
                ErrorCode.APP_QUERY_FAILED,
                "ADB failed while querying Android app metadata.",
                "Verify the device is online and retry android_list_apps.",
                data={"stage": "app_query", "arguments": arguments[:4]},
            ) from error
        if isinstance(output, bytes):
            text = output.decode("utf-8", errors="replace")
        elif isinstance(output, str):
            text = output
        else:
            raise MobileUseError(
                ErrorCode.APP_QUERY_FAILED,
                "ADB returned invalid Android app metadata.",
                "Retry android_list_apps after checking the device connection.",
                data={"stage": "app_query", "arguments": arguments[:4]},
            )
        if len(text.encode("utf-8")) > self.config.subprocess_max_output_bytes:
            raise MobileUseError(
                ErrorCode.APP_QUERY_FAILED,
                "Android app metadata exceeded the bounded query output budget.",
                "Retry with the device responsive; oversized metadata cannot be returned safely.",
                data={
                    "stage": "app_query",
                    "arguments": arguments[:4],
                    "output_limit_bytes": self.config.subprocess_max_output_bytes,
                },
            )
        if _is_shell_error_output(text):
            raise MobileUseError(
                ErrorCode.APP_QUERY_FAILED,
                "Android reported an error while querying app metadata.",
                "Verify the selected device is responsive and retry android_list_apps.",
                data={"stage": "app_query", "arguments": arguments[:4]},
            )
        return text

    async def list_app_inventory(
        self,
        query: str | None = None,
        *,
        include_system: bool = True,
    ) -> AppInventory:
        """Return stable Android package records without model-based matching."""

        package_output: str | None = None
        first_error: MobileUseError | None = None
        try:
            package_output = await self._app_shell(["pm", "list", "packages"])
        except MobileUseError as error:
            first_error = error
        packages = _parse_package_names(package_output or "")
        used_third_party_fallback = False
        if not packages:
            try:
                fallback_output = await self._app_shell(["pm", "list", "packages", "-3"])
            except MobileUseError as error:
                if first_error is not None:
                    raise MobileUseError(
                        ErrorCode.APP_QUERY_FAILED,
                        "ADB failed while listing installed Android apps.",
                        "Run `adb devices -l`, verify the selected device, and retry.",
                        data={"stage": "package_list"},
                    ) from error
                raise
            packages = _parse_package_names(fallback_output)
            used_third_party_fallback = bool(packages)
        elif first_error is not None:
            # A successful fallback is authoritative even if a newer package
            # manager command was unavailable on this Android build.
            used_third_party_fallback = True

        optional_errors: list[str] = []
        system_packages: set[str] = set()
        third_party_packages: set[str] = set(packages) if used_third_party_fallback else set()
        for arguments, target in (
            (["pm", "list", "packages", "-s"], system_packages),
            (["pm", "list", "packages", "-3"], third_party_packages),
        ):
            if used_third_party_fallback and arguments[-1] == "-3":
                continue
            try:
                target.update(_parse_package_names(await self._app_shell(arguments)))
            except MobileUseError as error:
                optional_errors.append(f"{arguments[-1]}:{error.code.value}")

        records: list[AppInfo] = []
        metadata_errors: list[str] = []
        for package in packages:
            package_errors: list[str] = []
            package_dump = ""
            try:
                package_dump = await self._app_shell(["dumpsys", "package", package])
            except MobileUseError as error:
                package_errors.append(f"dumpsys:{error.code.value}")
                metadata_errors.append(f"{package}:dumpsys:{error.code.value}")
            metadata = _parse_app_metadata(package_dump, package)
            launcher_label = metadata.launcher_label
            activity = None
            resolve_error: MobileUseError | None = None
            try:
                activity_output = await self._app_shell(
                    ["cmd", "package", "resolve-activity", "--brief", "--user", "0", package]
                )
            except MobileUseError as error:
                resolve_error = error
                activity_output = ""
            if launcher_label is None:
                launcher_label = _parse_label(activity_output)
            activity = _parse_activity(activity_output, package)
            if activity is None:
                # Older Android package managers use query-activities and
                # return the same component shape.  Try it even when the
                # preferred resolver command is unavailable.
                try:
                    activity_output = await self._app_shell(
                        [
                            "cmd",
                            "package",
                            "query-activities",
                            "--brief",
                            "--user",
                            "0",
                            "-a",
                            "android.intent.action.MAIN",
                            "-c",
                            "android.intent.category.LAUNCHER",
                            package,
                        ]
                    )
                except MobileUseError as error:
                    package_errors.append(f"activity:{error.code.value}")
                    metadata_errors.append(f"{package}:activity:{error.code.value}")
                    if resolve_error is not None and resolve_error.code != error.code:
                        metadata_errors.append(f"{package}:resolve:{resolve_error.code.value}")
                    activity_output = ""
                if launcher_label is None:
                    launcher_label = _parse_label(activity_output)
                activity = _parse_activity(activity_output, package)

            is_system = metadata.is_system
            if package in system_packages:
                is_system = True
            elif package in third_party_packages:
                is_system = False
            is_third_party = None if is_system is None else not is_system
            values_known = sum(
                value is not None
                for value in (
                    launcher_label,
                    activity or metadata.launchable_activity,
                    is_system,
                    metadata.enabled,
                )
            )
            optional_values_known = sum(
                value is not None
                for value in (
                    launcher_label,
                    activity or metadata.launchable_activity,
                    metadata.enabled,
                )
            )
            if package_errors and optional_values_known == 0:
                metadata_status = "unavailable"
            elif values_known < 4 or package_errors:
                metadata_status = "partial"
            else:
                metadata_status = "available"
            records.append(
                AppInfo(
                    package=package,
                    launcher_label=launcher_label,
                    launchable_activity=activity or metadata.launchable_activity,
                    is_system=is_system,
                    is_third_party=is_third_party,
                    enabled=metadata.enabled,
                    metadata_status=metadata_status,
                )
            )

        if query:
            normalized = query.casefold()
            records = [
                app
                for app in records
                if normalized in app.package.casefold()
                or (app.launcher_label is not None and normalized in app.launcher_label.casefold())
            ]
        if not include_system:
            records = [app for app in records if app.is_third_party is True]

        records.sort(key=lambda app: (app.package.casefold(), app.package))
        record_statuses = {app.metadata_status for app in records}
        if not packages:
            status = "available"
        elif record_statuses == {"unavailable"}:
            status = "unavailable"
        elif "partial" in record_statuses or optional_errors:
            status = "partial"
        else:
            status = "available"
        return AppInventory(
            apps=records,
            metadata_status=status,
            metadata_errors=(optional_errors + metadata_errors)[:50],
        )

    async def start_recording(
        self,
        max_duration_seconds: int,
        bit_rate: int | None = None,
    ) -> dict[str, object]:
        return await self.recording.start(max_duration_seconds, bit_rate)

    async def stop_recording(self, artifact_id: str | None = None) -> dict[str, object]:
        return await self.recording.stop(artifact_id)

    def bind_recording_context(self, session_id: str | None, generation: int | None) -> None:
        """Stamp recordings created by this controller with session provenance."""

        self.recording.bind_session(session_id, generation)

    def recording_status(self, operation_id: str | None = None) -> dict[str, object]:
        """Return the owned recording state without touching the Android device."""

        return self.recording.status(operation_id)

    async def retrieve_recording(self, artifact_id: str) -> dict[str, object]:
        """Claim one retained recording artifact by opaque ID."""

        return await self.recording.retrieve(artifact_id)

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
