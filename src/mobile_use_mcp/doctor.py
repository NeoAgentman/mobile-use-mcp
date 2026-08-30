"""Bounded Android runtime diagnostics.

The doctor intentionally reports a complete capability matrix.  A missing
ADB binary, an unauthorized device, and a missing optional recorder are
different operator outcomes; one failed probe must not prevent the remaining
checks from being attempted.  Probes never select a device when more than one
authorized device is online unless the caller supplies an exact serial.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from mobile_use_mcp.android_client import AndroidClient, AndroidDevice
from mobile_use_mcp.config import RuntimeConfig
from mobile_use_mcp.devices import DeviceRegistry
from mobile_use_mcp.errors import MobileUseError
from mobile_use_mcp.models import DeviceInfo, DeviceState, DoctorCheck, DoctorResult, DoctorStatus

U2Connector = Callable[[str], AndroidDevice]

CHECK_NAMES = (
    "adb",
    "device",
    "uiautomator2",
    "screenshot",
    "hierarchy",
    "screen_recording",
    "ffmpeg",
)


def _safe_message(error: BaseException, fallback: str) -> str:
    """Map expected probe errors to messages without raw command output."""

    if isinstance(error, MobileUseError):
        return error.message
    return fallback


def _check(
    name: str,
    status: DoctorStatus,
    message: str,
    started: float,
    details: dict[str, Any] | None = None,
) -> DoctorCheck:
    return DoctorCheck(
        name=name,
        status=status,
        message=message,
        duration_ms=max(0.0, round((time.monotonic() - started) * 1_000, 2)),
        details=details or {},
    )


def _unavailable(name: str, message: str, started: float, **details: Any) -> DoctorCheck:
    return _check(name, DoctorStatus.UNAVAILABLE, message, started, details)


def _degraded(name: str, message: str, started: float, **details: Any) -> DoctorCheck:
    return _check(name, DoctorStatus.DEGRADED, message, started, details)


def _ready(name: str, message: str, started: float, **details: Any) -> DoctorCheck:
    return _check(name, DoctorStatus.READY, message, started, details)


def _device_for_serial(devices: list[DeviceInfo], serial: str | None) -> DeviceInfo | None:
    if serial is None:
        online = [device for device in devices if device.state == DeviceState.DEVICE]
        return online[0] if len(online) == 1 else None
    return next((device for device in devices if device.serial == serial), None)


def run_doctor(
    config: RuntimeConfig | None = None,
    *,
    serial: str | None = None,
    registry: DeviceRegistry | None = None,
    u2_connector: U2Connector | None = None,
    operation_id: str = "doctor-local",
) -> DoctorResult:
    """Run all Android doctor probes synchronously with per-command bounds."""

    runtime = config or RuntimeConfig.defaults()
    device_registry = registry or DeviceRegistry(config=runtime)
    checks: dict[str, DoctorCheck] = {}
    selected: DeviceInfo | None = None

    started = time.monotonic()
    try:
        device_registry.check_adb()
    except Exception as error:
        checks["adb"] = _unavailable(
            "adb",
            _safe_message(error, "ADB availability could not be verified."),
            started,
        )
    else:
        checks["adb"] = _ready(
            "adb",
            "ADB is available and responded to a version probe.",
            started,
            host=runtime.adb_host,
            port=runtime.adb_port,
        )

    started = time.monotonic()
    devices: list[DeviceInfo] = []
    if checks["adb"].status == DoctorStatus.READY:
        try:
            devices = device_registry.list_devices()
        except Exception as error:
            checks["device"] = _unavailable(
                "device",
                _safe_message(error, "Android device state could not be read."),
                started,
            )
        else:
            selected = _device_for_serial(devices, serial)
            if serial is not None and selected is None:
                checks["device"] = _unavailable(
                    "device",
                    "The requested Android serial was not found.",
                    started,
                )
            elif serial is not None:
                assert selected is not None
                if selected.state != DeviceState.DEVICE:
                    checks["device"] = _degraded(
                        "device",
                        f"The requested Android device is {selected.state.value}.",
                        started,
                        state=selected.state.value,
                    )
                else:
                    checks["device"] = _ready(
                        "device",
                        "The requested Android device is authorized and online.",
                        started,
                        state=selected.state.value,
                    )
            else:
                online = [device for device in devices if device.state == DeviceState.DEVICE]
                if len(online) > 1 and serial is None:
                    checks["device"] = _degraded(
                        "device",
                        "Multiple authorized Android devices are online; no device was selected.",
                        started,
                        online_count=len(online),
                    )
                elif selected is not None and selected.state == DeviceState.DEVICE:
                    checks["device"] = _ready(
                        "device",
                        "One authorized Android device is ready for probes.",
                        started,
                        state=selected.state.value,
                    )
                elif devices:
                    states = sorted({device.state.value for device in devices})
                    checks["device"] = _degraded(
                        "device",
                        "Android devices were found, but none is authorized and online.",
                        started,
                        states=states,
                    )
                else:
                    checks["device"] = _unavailable(
                        "device",
                        "No Android device was found.",
                        started,
                    )
    else:
        checks["device"] = _unavailable(
            "device",
            "ADB is unavailable, so Android device state cannot be checked.",
            started,
        )

    android_device: AndroidDevice | None = None
    probe_client: AndroidClient | None = None
    started = time.monotonic()
    if selected is None or selected.state != DeviceState.DEVICE:
        checks["uiautomator2"] = _unavailable(
            "uiautomator2",
            "No unambiguous authorized Android device is available for uiautomator2.",
            started,
        )
    else:
        try:
            if u2_connector is not None:
                android_device = u2_connector(selected.serial)
            else:
                probe_client = AndroidClient(selected.serial, config=runtime)
                android_device = probe_client.ensure_connected()
            _ = android_device.info
        except Exception:
            checks["uiautomator2"] = _unavailable(
                "uiautomator2",
                "uiautomator2 could not connect to the selected Android device.",
                started,
            )
        else:
            checks["uiautomator2"] = _ready(
                "uiautomator2",
                "uiautomator2 connected and returned device information.",
                started,
            )

    started = time.monotonic()
    if android_device is None:
        checks["screenshot"] = _unavailable(
            "screenshot",
            "uiautomator2 is unavailable, so screenshot capture was not attempted.",
            started,
        )
    else:
        try:
            image = android_device.screenshot()
            if image is None:
                raise RuntimeError("empty screenshot")
        except Exception:
            checks["screenshot"] = _unavailable(
                "screenshot",
                "The selected Android device did not return a screenshot.",
                started,
            )
        else:
            checks["screenshot"] = _ready(
                "screenshot",
                "Screenshot capture succeeded in memory.",
                started,
            )

    started = time.monotonic()
    if android_device is None:
        checks["hierarchy"] = _unavailable(
            "hierarchy",
            "uiautomator2 is unavailable, so hierarchy capture was not attempted.",
            started,
        )
    else:
        try:
            hierarchy = android_device.dump_hierarchy(compressed=True)
            if not hierarchy.strip():
                raise RuntimeError("empty hierarchy")
        except Exception:
            checks["hierarchy"] = _unavailable(
                "hierarchy",
                "The selected Android device did not return a UI hierarchy.",
                started,
            )
        else:
            checks["hierarchy"] = _ready(
                "hierarchy",
                "UIAutomator hierarchy capture succeeded in memory.",
                started,
            )

    # The concrete uiautomator2 object owns a socket in some releases.  It is
    # deliberately closed after all dependent probes, while injected fakes
    # are closed only when they advertise an optional close method.
    if probe_client is not None:
        probe_client.disconnect()
    elif android_device is not None:
        close = getattr(android_device, "close", None)
        if callable(close):
            with suppress(Exception):
                close()

    started = time.monotonic()
    if selected is None or selected.state != DeviceState.DEVICE:
        checks["screen_recording"] = _unavailable(
            "screen_recording",
            "No unambiguous authorized Android device is available for screen recording.",
            started,
        )
    else:
        try:
            availability = device_registry.shell(selected.serial, ("command", "-v", "screenrecord"))
        except Exception:
            checks["screen_recording"] = _unavailable(
                "screen_recording",
                "The Android screenrecord capability could not be checked.",
                started,
            )
        else:
            if "screenrecord" in availability:
                checks["screen_recording"] = _ready(
                    "screen_recording",
                    "The Android screenrecord command is available.",
                    started,
                )
            else:
                checks["screen_recording"] = _degraded(
                    "screen_recording",
                    "This Android build does not provide screenrecord.",
                    started,
                )

    started = time.monotonic()
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        checks["ffmpeg"] = _degraded(
            "ffmpeg",
            "ffmpeg is unavailable; multi-segment recordings cannot be merged.",
            started,
        )
    else:
        checks["ffmpeg"] = _ready("ffmpeg", "ffmpeg is available on the MCP host.", started)

    ordered_checks = [checks[name] for name in CHECK_NAMES]
    statuses = {check.status for check in ordered_checks}
    if DoctorStatus.UNAVAILABLE in statuses:
        overall = DoctorStatus.UNAVAILABLE
    elif DoctorStatus.DEGRADED in statuses:
        overall = DoctorStatus.DEGRADED
    else:
        overall = DoctorStatus.READY
    return DoctorResult(
        success=overall == DoctorStatus.READY,
        operation_id=operation_id,
        overall_status=overall,
        message=(
            "Android runtime is ready."
            if overall == DoctorStatus.READY
            else "Android runtime diagnosis completed with degraded or unavailable capabilities."
        ),
        serial=selected.serial if selected is not None else serial,
        checks=ordered_checks,
        config={
            "adb_host": runtime.adb_host,
            "adb_port": runtime.adb_port,
            "operation_timeout_seconds": runtime.operation_timeout_seconds,
            "queue_timeout_seconds": runtime.queue_timeout_seconds,
        },
    )


async def run_doctor_bounded(
    config: RuntimeConfig | None = None,
    *,
    serial: str | None = None,
    registry: DeviceRegistry | None = None,
    u2_connector: U2Connector | None = None,
    operation_id: str = "doctor-local",
) -> DoctorResult:
    """Run the synchronous probes without allowing a tool call to hang."""

    runtime = config or RuntimeConfig.defaults()
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                run_doctor,
                runtime,
                serial=serial,
                registry=registry,
                u2_connector=u2_connector,
                operation_id=operation_id,
            ),
            timeout=runtime.operation_timeout_seconds,
        )
    except TimeoutError:
        checks = [
            DoctorCheck(
                name=name,
                status=DoctorStatus.UNAVAILABLE,
                message="The bounded doctor operation timed out before this probe completed.",
                duration_ms=runtime.operation_timeout_seconds * 1_000,
            )
            for name in CHECK_NAMES
        ]
        return DoctorResult(
            success=False,
            operation_id=operation_id,
            overall_status=DoctorStatus.UNAVAILABLE,
            message="Android runtime diagnosis exceeded its configured operation budget.",
            serial=serial,
            checks=checks,
            config={
                "adb_host": runtime.adb_host,
                "adb_port": runtime.adb_port,
            },
        )
