"""Deterministic Android operations used by MCP tool handlers."""

import asyncio
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
    AppLaunchAttempt,
    AppLaunchResult,
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
