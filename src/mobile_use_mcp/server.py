"""stdio MCP server entry point and Android observation tools."""

import asyncio
import hashlib
import json
import signal
import sys
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import isawaitable
from time import monotonic
from typing import Annotated, Any, Literal, cast

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from mobile_use_mcp.config import (
    ConfigurationError,
    RuntimeConfig,
    format_startup_error,
    load_runtime_config,
)
from mobile_use_mcp.controller import InputOutcome, normalized_coordinate_to_pixel
from mobile_use_mcp.doctor import run_doctor_bounded
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.images import encode_screenshot
from mobile_use_mcp.models import (
    AndroidPackageName,
    AndroidSystemSurface,
    AppLaunchResult,
    AppListResult,
    AppTerminationResult,
    DeviceConnectResult,
    DeviceDisconnectResult,
    DeviceInfo,
    DeviceListResult,
    DeviceStatusResult,
    DoctorResult,
    DoctorStatus,
    ForegroundApp,
    LifecycleError,
    LifecycleResult,
    OperationResult,
    PackagePolicy,
    RecordingArtifact,
    RecordingState,
    RecordingStatusResult,
    ScreenshotCapture,
    ScreenshotResult,
    ScreenSnapshot,
    SessionConnection,
    SessionLifecycle,
    SnapshotResult,
    SwipePercentage,
    Target,
    UIElement,
    WaitResult,
)
from mobile_use_mcp.processes import run_blocking
from mobile_use_mcp.session import DeviceSession
from mobile_use_mcp.version import __version__

MCP_INSTRUCTIONS = """Use this MCP whenever the user asks to interact with a physical Android
phone or an installed mobile app. This includes requests phrased as 手机, 安卓手机, 手机 App,
打开某个 App, 浏览或阅读 App 内容, 查看评论, 采集页面数据, 输入文字, 点击, 滑动, 截图, or
screen recording. Connect the device, resolve the installed package with android_list_apps when
possible, and compose snapshot/tap/swipe/input tools for multi-step mobile workflows.
Observe again after actions. The tools operate Android only, not iOS. Screenshots capture visible
pixels but do not download an app's original remote media file unless another tool provides it.
Recording artifacts are owned by the server: use the returned artifact_id with
android_retrieve_recording, never submit a local filesystem path for cleanup.
Call android_doctor to diagnose the local ADB, device, uiautomator2, and capability state before
starting a workflow. Use android_wait_for_text, android_wait_for_element, or
android_wait_for_ui_change for bounded condition synchronization; android_wait is only a fixed
delay and does not inspect UI state. When a package scope is requested, declare exact
allowed_packages and any required allowed_system_packages or allowed_system_surfaces on
android_connect; never infer a system-surface exception from the screen."""

runtime_config = RuntimeConfig.defaults()
session = DeviceSession(config=runtime_config)


def configure_runtime(config: RuntimeConfig) -> None:
    """Install the one frozen configuration before serving stdio requests."""

    global runtime_config, session
    runtime_config = config
    session = DeviceSession(config=config)


@asynccontextmanager
async def server_lifespan(_server: Any) -> AsyncGenerator[None, None]:
    """Close the owned Android session when the MCP server exits or is cancelled."""

    try:
        yield None
    finally:
        await session.close()


mcp = FastMCP("mobile_use_mcp", instructions=MCP_INSTRUCTIONS, lifespan=server_lifespan)
# FastMCP currently derives ``serverInfo.version`` from the MCP SDK package and
# does not expose a constructor argument for an application version. Override
# the low-level value so initialize identifies this distribution consistently.
mcp._mcp_server.version = __version__  # pyright: ignore[reportPrivateUsage]

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
SESSION_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
OPEN_WORLD_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)


def _redact_secret(value: Any, secret: str | None) -> Any:
    """Replace a typed payload in nested diagnostics without logging it."""

    if not secret:
        return value
    if isinstance(value, BaseModel):
        return _redact_secret(value.model_dump(mode="json"), secret)
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, list):
        items = cast(list[Any], value)
        return [_redact_secret(item, secret) for item in items[:50]]
    if isinstance(value, tuple):
        items = cast(tuple[Any, ...], value)
        return tuple(_redact_secret(item, secret) for item in items[:50])
    if isinstance(value, dict):
        mapping = cast(dict[Any, Any], value)
        return {str(key): _redact_secret(item, secret) for key, item in mapping.items()}
    return value


def _redact_exact(value: Any, secret: str | None) -> Any:
    """Redact exact values in a known schema without corrupting field names."""

    if not secret:
        return value
    if isinstance(value, str):
        return "[REDACTED]" if value == secret else value
    if isinstance(value, list):
        items = cast(list[Any], value)
        return [_redact_exact(item, secret) for item in items[:50]]
    if isinstance(value, tuple):
        items = cast(tuple[Any, ...], value)
        return tuple(_redact_exact(item, secret) for item in items[:50])
    if isinstance(value, dict):
        mapping = cast(dict[Any, Any], value)
        return {str(key): _redact_exact(item, secret) for key, item in mapping.items()}
    return value


def _failure(error: MobileUseError, *, secret: str | None = None) -> OperationResult:
    return OperationResult(
        success=False,
        error_code=error.code,
        message=_redact_secret(error.message, secret),
        suggestion=_redact_secret(error.suggestion, secret),
        data=_redact_secret(error.data, secret),
    )


def _recording_result(
    details: Any,
    *,
    operation_id: str,
    default_state: RecordingState,
    message: str,
) -> RecordingStatusResult:
    """Normalize legacy controller dictionaries into the recording schema."""

    if isinstance(details, RecordingStatusResult):
        return details.model_copy(update={"operation_id": operation_id}, deep=True)
    raw: dict[str, Any] = cast(dict[str, Any], details) if isinstance(details, dict) else {}
    raw_artifact: Any = raw.get("artifact")
    artifact: RecordingArtifact | None = None
    if isinstance(raw_artifact, RecordingArtifact):
        artifact = raw_artifact
    elif isinstance(raw_artifact, dict):
        with suppress(Exception):
            artifact = RecordingArtifact.model_validate(raw_artifact)
    raw_state: Any = raw.get("state", default_state)
    try:
        state = (
            raw_state
            if isinstance(raw_state, RecordingState)
            else RecordingState(str(raw_state))
        )
    except ValueError:
        state = default_state
    warnings_value: Any = raw.get("warnings", artifact.warnings if artifact is not None else [])
    warning_items = cast(list[Any], warnings_value) if isinstance(warnings_value, list) else []
    warnings = [str(value) for value in warning_items]
    segment_paths_value: Any = raw.get(
        "segment_paths", artifact.segment_paths if artifact is not None else []
    )
    segment_path_items = (
        cast(list[Any], segment_paths_value) if isinstance(segment_paths_value, list) else []
    )
    segment_paths = [str(value) for value in segment_path_items]
    data: Any = raw.get("data")
    compatibility_data: dict[str, Any] = (
        cast(dict[str, Any], data) if isinstance(data, dict) else dict(raw)
    )
    return RecordingStatusResult(
        success=bool(raw.get("success", True)),
        operation_id=operation_id,
        message=str(raw.get("message", message)),
        state=state,
        serial=cast(str | None, raw.get("serial")),
        session_id=cast(str | None, raw.get("session_id")),
        generation=cast(int | None, raw.get("generation")),
        artifact_id=cast(
            str | None,
            raw.get("artifact_id", artifact.artifact_id if artifact is not None else None),
        ),
        artifact=artifact,
        max_duration_seconds=cast(int | None, raw.get("max_duration_seconds")),
        bit_rate=cast(int | None, raw.get("bit_rate")),
        duration_seconds=cast(
            float | None,
            raw.get(
                "duration_seconds", artifact.duration_seconds if artifact is not None else None
            ),
        ),
        recording_path=cast(
            str | None,
            raw.get("recording_path", artifact.recording_path if artifact is not None else None),
        ),
        segment_paths=segment_paths,
        segment_count=int(raw.get("segment_count", artifact.segment_count if artifact else 0)),
        warnings=warnings[:20],
        retrieved=bool(raw.get("retrieved", False)),
        error_code=cast(str | None, raw.get("error_code")),
        category=cast(str | None, raw.get("category")),
        retryable=cast(bool | None, raw.get("retryable")),
        suggestion=cast(str | None, raw.get("suggestion")),
        details=cast(dict[str, Any], raw.get("details", {}))
        if isinstance(raw.get("details", {}), dict)
        else {},
        data=compatibility_data,
    )


def _recording_failure(
    error: MobileUseError,
    *,
    operation_id: str,
    status: RecordingStatusResult | None = None,
) -> RecordingStatusResult:
    """Build a typed recording failure while keeping local paths bounded."""

    current = status or session.recording_status(operation_id)
    details = _redact_secret(error.data, None)
    lifecycle_error = LifecycleError(
        code=error.code.value,
        category=error.category.value,
        retryable=error.retryable,
        message=error.message,
        suggestion=error.suggestion,
        details=cast(dict[str, Any], details) if isinstance(details, dict) else {},
    )
    return RecordingStatusResult(
        success=False,
        operation_id=operation_id,
        message=error.message,
        state=current.state,
        serial=current.serial,
        session_id=current.session_id,
        generation=current.generation,
        artifact_id=current.artifact_id,
        artifact=current.artifact,
        warnings=current.warnings,
        error_code=error.code.value,
        category=error.category.value,
        retryable=error.retryable,
        suggestion=error.suggestion,
        details=lifecycle_error.details,
        error=lifecycle_error,
        data=cast(dict[str, Any], details) if isinstance(details, dict) else {},
    )


class _LifecycleCallToolResult(CallToolResult):
    """MCP error result that also supports legacy direct-call mapping access."""

    def __getitem__(self, key: str) -> Any:
        if self.structuredContent is None:
            raise KeyError(key)
        return self.structuredContent[key]


def _lifecycle_failure(
    output_type: type[LifecycleResult],
    *,
    operation_id: str,
    error: MobileUseError,
    state: SessionLifecycle,
    session_id: str | None = None,
    generation: int | None = None,
    device: DeviceInfo | None = None,
    connected: bool = False,
    data: dict[str, Any] | None = None,
) -> _LifecycleCallToolResult:
    """Build one typed lifecycle failure and mark it as an MCP business error."""

    def safe_value(value: Any, depth: int = 0) -> Any:
        if depth > 2:
            return None
        if isinstance(value, str):
            return value[:512]
        if isinstance(value, int | float | bool) or value is None:
            return value
        if isinstance(value, list):
            items = cast(list[Any], value)
            return [safe_value(item, depth + 1) for item in items[:50]]
        if isinstance(value, dict):
            nested = cast(dict[Any, Any], value)
            return {
                str(key): safe_value(item, depth + 1) for key, item in list(nested.items())[:50]
            }
        return None

    safe_details = {str(key): safe_value(value) for key, value in error.data.items()}
    lifecycle_error = LifecycleError(
        code=error.code.value,
        category=error.category.value,
        retryable=error.retryable,
        message=error.message,
        suggestion=error.suggestion,
        details=safe_details,
    )
    result = output_type(
        success=False,
        operation_id=operation_id,
        message=error.message,
        state=state,
        session_id=session_id,
        generation=generation,
        device=device,
        connected=connected,
        error_code=error.code.value,
        category=error.category.value,
        retryable=error.retryable,
        suggestion=error.suggestion,
        details=lifecycle_error.details,
        error=lifecycle_error,
        data=data or {},
    )
    payload = result.model_dump(mode="json")
    return _LifecycleCallToolResult(
        isError=True,
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structuredContent=payload,
    )


def _as_lifecycle_error_result(result: LifecycleResult) -> _LifecycleCallToolResult:
    payload = result.model_dump(mode="json")
    return _LifecycleCallToolResult(
        isError=True,
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structuredContent=payload,
    )


def _image_delivery_metadata(
    tool_name: str,
    image_format: Literal["png", "jpeg"],
    image_quality: int,
) -> dict[str, Any]:
    if image_format == "png":
        return {
            "image_quality": None,
            "image_lossless": True,
            "quality_notice": "This is the original-resolution lossless PNG image.",
            "lossless_fallback": None,
        }
    return {
        "image_quality": image_quality,
        "image_lossless": False,
        "quality_notice": (
            "This is a lossy JPEG at the original device resolution. If small text, icons, "
            f"tables, or visual details are unclear, call {tool_name} again with "
            'image_format="png" for the original lossless image.'
        ),
        "lossless_fallback": {
            "tool": tool_name,
            "arguments": {"image_format": "png"},
        },
    }


async def _capture_screenshot_only(controller: Any) -> ScreenshotCapture:
    """Use the hierarchy-free controller seam with legacy fake fallback.

    ``Mock`` instances dynamically manufacture attributes, so merely calling
    ``getattr(controller, "screenshot")`` would make old tests accidentally
    take a mock path.  Concrete controller methods and explicitly configured
    async test methods are both accepted; an unconfigured legacy fake falls
    back to its existing combined snapshot method.
    """

    def available(name: str) -> Any:
        instance_method = getattr(controller, name, None)
        controller_type = cast(type[Any], type(controller))
        if getattr(controller_type, name, None) is not None or name in getattr(
            controller, "__dict__", {}
        ):
            return instance_method
        if hasattr(instance_method, "assert_awaited"):
            return instance_method
        return None

    method = available("capture_screenshot") or available("screenshot")
    if method is not None:
        result = method()
        if isawaitable(result):
            result = await result
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
                serial=str(getattr(controller, "serial", "android")),
                screenshot_png=screenshot,
                width=width,
                height=height,
            )

    # Source-compatible fallback for an injected pre-revision controller.
    legacy = controller.snapshot
    result = legacy()
    if isawaitable(result):
        result = await result
    if isinstance(result, ScreenSnapshot):
        return ScreenshotCapture(
            serial=result.serial,
            screenshot_png=result.screenshot_png,
            width=result.width,
            height=result.height,
        )
    raise TypeError("Android controller did not return screenshot data")


@mcp.tool(
    name="android_list_devices",
    title="List Android devices",
    annotations=READ_ONLY,
    structured_output=True,
)
async def android_list_devices(limit: int = 50, offset: int = 0) -> DeviceListResult:
    """List Android devices known to ADB, including offline and unauthorized devices."""

    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    operation_id = session.new_operation_id()
    try:
        devices = await session.run_operation(
            "list_devices",
            lambda context: session.alist_devices(operation_id=context.operation_id),
            operation_id=operation_id,
            retry_safe=True,
            mutating=False,
        )
    except MobileUseError as error:
        return cast(
            DeviceListResult,
            _lifecycle_failure(
                DeviceListResult,
                operation_id=operation_id,
                error=error,
                state=session.state,
            ),
        )
    page = devices[offset : offset + limit]
    return DeviceListResult(
        success=True,
        operation_id=operation_id,
        message=f"Found {len(devices)} Android device(s).",
        state=session.state,
        total=len(devices),
        count=len(page),
        offset=offset,
        limit=limit,
        has_more=offset + len(page) < len(devices),
        next_offset=offset + len(page) if offset + len(page) < len(devices) else None,
        devices=page,
    )


@mcp.tool(
    name="android_doctor",
    title="Diagnose Android runtime",
    annotations=READ_ONLY,
    structured_output=True,
)
async def android_doctor(
    serial: Annotated[
        str | None,
        Field(
            max_length=256,
            description=(
                "Optional exact serial from android_list_devices. Required to probe a device "
                "when more than one authorized device is online."
            ),
        ),
    ] = None,
) -> DoctorResult:
    """Diagnose ADB, device authorization, uiautomator2, and device capabilities.

    Every check is independent and bounded.  The doctor never silently chooses
    among multiple online devices; pass an exact serial when that is required.
    Missing optional capabilities are reported as degraded rather than hiding
    the rest of the matrix behind one generic failure.
    """

    operation_id = session.new_operation_id()
    try:
        return await session.run_operation(
            "doctor",
            lambda context: run_doctor_bounded(
                session.config,
                serial=serial,
                registry=session.registry,
                operation_id=context.operation_id,
            ),
            operation_id=operation_id,
            retry_safe=True,
            mutating=False,
        )
    except MobileUseError as error:
        return DoctorResult(
            success=False,
            operation_id=operation_id,
            overall_status=DoctorStatus.UNAVAILABLE,
            message=error.message,
            serial=serial,
            error_code=error.code.value,
            category=error.category.value,
            retryable=error.retryable,
            suggestion=error.suggestion,
            details=error.data,
            config={
                "adb_host": session.config.adb_host,
                "adb_port": session.config.adb_port,
            },
        )


@mcp.tool(
    name="android_connect",
    title="Connect Android device",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_connect(
    serial: Annotated[
        str | None,
        Field(
            max_length=256,
            description=(
                "Exact serial from android_list_devices. Required when multiple devices are online."
            ),
        ),
    ] = None,
    policy: Annotated[
        PackagePolicy | None,
        Field(
            description=(
                "Optional strict package allowlist. An instantiated empty policy denies all "
                "packages until an explicit package or system surface is listed."
            )
        ),
    ] = None,
    package_policy: Annotated[
        PackagePolicy | None,
        Field(
            description=(
                "Compatibility alias for policy; do not provide both policy and package_policy."
            )
        ),
    ] = None,
    allowed_packages: Annotated[
        list[AndroidPackageName] | None,
        Field(
            max_length=100,
            description="Exact Android app package names allowed by this connection.",
        ),
    ] = None,
    allowed_app_packages: Annotated[
        list[AndroidPackageName] | None,
        Field(
            max_length=100,
            description="Compatibility alias for allowed_packages.",
        ),
    ] = None,
    allowed_system_packages: Annotated[
        list[AndroidPackageName] | None,
        Field(
            max_length=100,
            description="Exact Android system package names explicitly allowed by this connection.",
        ),
    ] = None,
    system_packages: Annotated[
        list[AndroidPackageName] | None,
        Field(max_length=100, description="Compatibility alias for allowed_system_packages."),
    ] = None,
    allowed_system_surfaces: Annotated[
        list[AndroidSystemSurface] | None,
        Field(
            max_length=20,
            description=(
                "Named Android system/action surfaces explicitly allowed: permission_dialog, "
                "chooser, system_ui, launcher, or browser."
            ),
        ),
    ] = None,
    system_surfaces: Annotated[
        list[AndroidSystemSurface] | None,
        Field(max_length=20, description="Compatibility alias for allowed_system_surfaces."),
    ] = None,
    allow_unrestricted: Annotated[
        bool,
        Field(
            description=(
                "Explicitly clear a restrictive policy while reconnecting; this cannot be "
                "combined with any package policy declaration."
            )
        ),
    ] = False,
) -> DeviceConnectResult:
    """Connect this single-device MCP session to one authorized Android device.

    Specify serial when multiple devices are online. Reconnecting replaces the current device and
    invalidates any cached snapshot; a new MCP process does not inherit this session.
    """

    operation_id = session.new_operation_id()
    try:
        connect_options: dict[str, Any] = {}
        if policy is not None:
            connect_options["policy"] = policy
        if package_policy is not None:
            connect_options["package_policy"] = package_policy
        if allowed_packages is not None:
            connect_options["allowed_packages"] = allowed_packages
        if allowed_app_packages is not None:
            connect_options["allowed_app_packages"] = allowed_app_packages
        if allowed_system_packages is not None:
            connect_options["allowed_system_packages"] = allowed_system_packages
        if system_packages is not None:
            connect_options["system_packages"] = system_packages
        if allowed_system_surfaces is not None:
            connect_options["allowed_system_surfaces"] = allowed_system_surfaces
        if system_surfaces is not None:
            connect_options["system_surfaces"] = system_surfaces
        if allow_unrestricted:
            connect_options["allow_unrestricted"] = True
        connection = cast(
            SessionConnection | DeviceInfo,
            await session.run_operation(
                "connect",
                lambda context: run_blocking(
                    session.connect,
                    serial,
                    timeout_seconds=session.config.subprocess_timeout_seconds,
                    **connect_options,
                ),
                operation_id=operation_id,
                mutating=True,
            ),
        )
    except MobileUseError as error:
        return cast(
            DeviceConnectResult,
            _lifecycle_failure(
                DeviceConnectResult,
                operation_id=operation_id,
                error=error,
                state=session.state,
                session_id=session.session_id,
                generation=session.generation,
                device=session.device,
            ),
        )
    except Exception:
        error = MobileUseError(
            ErrorCode.OPERATION_FAILED,
            "Failed to initialize the Android automation connection.",
            "Check `adb devices -l`, unlock the device, and try again.",
        )
        return cast(
            DeviceConnectResult,
            _lifecycle_failure(
                DeviceConnectResult,
                operation_id=operation_id,
                error=error,
                state=session.state,
                session_id=session.session_id,
                generation=session.generation,
                device=session.device,
            ),
        )

    if isinstance(connection, SessionConnection):
        device = connection.device
        connection_session_id = connection.session_id
        connection_generation = connection.generation
        connection_state = connection.state
    else:
        # Compatibility for embedders that replaced the old core method with
        # a DeviceInfo-returning adapter.
        device = connection
        connection_session_id = session.session_id
        connection_generation = session.generation
        connection_state = session.state
    return DeviceConnectResult(
        success=True,
        operation_id=operation_id,
        message=f"Connected to Android device {device.serial}.",
        state=connection_state,
        session_id=connection_session_id,
        generation=connection_generation,
        device=device,
        connected=True,
        policy=(
            connection.policy
            if isinstance(connection, SessionConnection) and connection.policy is not None
            else session.policy_summary()
        ),
        data={
            "device": device.model_dump(mode="json"),
            "session_id": connection_session_id,
            "generation": connection_generation,
            "policy": session.policy_summary().model_dump(mode="json"),
            "package_policy": session.policy_summary().model_dump(mode="json"),
        },
    )


@mcp.tool(
    name="android_status",
    title="Get Android session status",
    annotations=READ_ONLY,
    structured_output=True,
)
async def android_status() -> DeviceStatusResult:
    """Return the lifecycle and safe device status of this MCP session."""

    operation_id = session.new_operation_id()
    try:
        return await session.run_operation(
            "status",
            lambda context: session.status(context.operation_id),
            operation_id=operation_id,
            retry_safe=True,
            mutating=False,
        )
    except MobileUseError as error:
        return cast(
            DeviceStatusResult,
            _lifecycle_failure(
                DeviceStatusResult,
                operation_id=operation_id,
                error=error,
                state=session.state,
                session_id=session.session_id,
                generation=session.generation,
                device=session.device,
            ),
        )


@mcp.tool(
    name="android_disconnect",
    title="Disconnect Android device",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_disconnect() -> DeviceDisconnectResult:
    """Disconnect the current session and clear cached snapshots and recording state."""

    try:
        result = await session.close()
    except MobileUseError as error:
        return cast(
            DeviceDisconnectResult,
            _lifecycle_failure(
                DeviceDisconnectResult,
                operation_id=str(error.data.get("operation_id", session.new_operation_id())),
                error=error,
                state=session.state,
                session_id=session.session_id,
                generation=session.generation,
                device=session.device,
            ),
        )
    except Exception:
        error = MobileUseError(
            ErrorCode.OPERATION_FAILED,
            "Failed to close the Android automation connection.",
            "Retry android_disconnect; if it persists, restart the local MCP server.",
        )
        return cast(
            DeviceDisconnectResult,
            _lifecycle_failure(
                DeviceDisconnectResult,
                operation_id=session.new_operation_id(),
                error=error,
                state=session.state,
            ),
        )
    if result.success:
        return result
    return cast(DeviceDisconnectResult, _as_lifecycle_error_result(result))


@mcp.tool(
    name="android_snapshot",
    title="Observe Android screen",
    annotations=READ_ONLY,
)
async def android_snapshot(
    detail_level: Annotated[
        Literal["compact", "full"],
        Field(
            description=(
                "compact returns at most max_elements and is the default. If truncated, first "
                "page or query the same snapshot_id with android_get_ui_elements; use full only "
                "as an explicit large-output fallback."
            )
        ),
    ] = "compact",
    interactive_only: Annotated[
        bool,
        Field(
            description=(
                "Return only clickable, focusable, or scrollable nodes. Keep false when reading "
                "static text such as product descriptions, articles, prices, or tables."
            )
        ),
    ] = False,
    max_elements: Annotated[
        int,
        Field(
            ge=1,
            le=500,
            description="Maximum nodes returned by compact mode; ignored by full mode.",
        ),
    ] = 200,
    max_text_length: Annotated[
        int,
        Field(
            ge=1,
            le=2_000,
            description=(
                "Maximum characters retained per node text/content_description, not a total "
                "response limit. Full mode still applies it; increase for long-form content."
            ),
        ),
    ] = 500,
    image_format: Annotated[
        Literal["png", "jpeg"],
        Field(description="JPEG is smaller; use PNG to retry with original lossless quality."),
    ] = "jpeg",
    image_quality: Annotated[
        int,
        Field(ge=1, le=100, description="JPEG quality only; ignored for lossless PNG."),
    ] = 60,
) -> CallToolResult:
    """Read visible content from the current Android app and capture its screen.

    Use this for mobile browsing and data collection such as titles, counts, descriptions, lists,
    comments, buttons, and form fields. Defaults to compact mode with at most 200 nodes. A truncated
    result includes snapshot_id and next_offset: prefer android_get_ui_elements on that unchanged
    snapshot for filtering or pagination, and use full only when focused retrieval is insufficient.
    Actions and waits advance the screen revision and invalidate copied element refs/bounds;
    retained snapshot IDs remain pageable until TTL or capacity eviction.
    """

    if not 1 <= max_elements <= 500:
        raise ValueError("max_elements must be between 1 and 500")
    if not 1 <= max_text_length <= 2_000:
        raise ValueError("max_text_length must be between 1 and 2000")
    # Runtime configuration is an upper bound even when a caller uses the
    # backwards-compatible tool defaults.  This keeps payload growth under
    # operator control without changing the public schema defaults.
    effective_max_elements = min(max_elements, session.config.snapshot_max_elements)
    effective_max_text_length = min(max_text_length, session.config.snapshot_max_text_length)
    operation_id = session.new_operation_id()
    try:

        async def capture(context: Any) -> tuple[Any, str]:
            context.checkpoint("observation")
            # Take provenance before the potentially slow Android read.  A
            # mutation that advances the revision before this callback
            # commits will reject the late observation.
            observation_context = session.observation_context()
            controller = session.require_controller()
            snapshot = await controller.snapshot(
                interactive_only=interactive_only,
                max_elements=None,
                max_text_length=effective_max_text_length,
            )
            context.checkpoint("snapshot_commit")
            snapshot_id = session.store_snapshot(
                snapshot,
                expected_context=observation_context,
            )
            return session.get_snapshot(snapshot_id), snapshot_id

        snapshot, snapshot_id = await session.run_operation(
            "snapshot",
            capture,
            operation_id=operation_id,
            retry_safe=True,
            mutating=False,
        )
    except MobileUseError as error:
        failure = _failure(error).model_dump(mode="json")
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=json.dumps(failure, ensure_ascii=False))],
            structuredContent=failure,
        )
    except Exception:
        failure = OperationResult(
            success=False,
            error_code="OPERATION_FAILED",
            message="Failed to capture the Android screen.",
            suggestion="Verify the device is connected, unlocked, and responsive, then retry.",
        ).model_dump(mode="json")
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=json.dumps(failure, ensure_ascii=False))],
            structuredContent=failure,
        )

    total_elements = len(snapshot.elements)
    returned_elements = (
        snapshot.elements if detail_level == "full" else snapshot.elements[:effective_max_elements]
    )
    truncated = len(returned_elements) < total_elements
    image_data, image_width, image_height = encode_screenshot(
        snapshot.screenshot_png,
        image_format=image_format,
        image_quality=image_quality,
    )
    metadata = SnapshotResult(
        success=True,
        snapshot_id=snapshot_id,
        operation_id=operation_id,
        session_id=snapshot.session_id,
        generation=snapshot.generation,
        screen_revision=snapshot.screen_revision,
        captured_at=snapshot.captured_at,
        detail_level=detail_level,
        serial=snapshot.serial,
        width=snapshot.width,
        height=snapshot.height,
        image_format=image_format,
        image_width=image_width,
        image_height=image_height,
        **_image_delivery_metadata("android_snapshot", image_format, image_quality),
        hierarchy_collected=not any(
            warning.startswith("HIERARCHY_UNAVAILABLE") for warning in snapshot.warnings
        ),
        foreground_app_collected=not any(
            warning.startswith("FOREGROUND_APP_UNAVAILABLE") for warning in snapshot.warnings
        ),
        foreground_app=snapshot.foreground_app,
        warnings=snapshot.warnings,
        total_elements=total_elements,
        returned_elements=len(returned_elements),
        element_count=len(returned_elements),
        truncated=truncated,
        next_offset=len(returned_elements) if truncated else None,
        full_fallback=(
            {"tool": "android_snapshot", "arguments": {"detail_level": "full"}}
            if truncated
            else None
        ),
        elements=returned_elements,
    ).model_dump(mode="json")
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)),
            Image(data=image_data, format=image_format).to_image_content(),
        ],
        structuredContent=metadata,
    )


@mcp.tool(
    name="android_screenshot",
    title="Capture Android screenshot",
    annotations=READ_ONLY,
)
async def android_screenshot(
    image_format: Annotated[
        Literal["png", "jpeg"],
        Field(description="JPEG is smaller; use PNG to retry with original lossless quality."),
    ] = "jpeg",
    image_quality: Annotated[
        int,
        Field(ge=1, le=100, description="JPEG quality only; ignored for lossless PNG."),
    ] = 60,
) -> CallToolResult:
    """Return only an original-resolution screenshot and basic metadata.

    This does not cache a pageable UI hierarchy. Use android_snapshot when locating targets or
    reading UI text; use this tool for visual-only inspection with lower structured context.
    """

    operation_id = session.new_operation_id()
    try:

        async def capture(_context: Any) -> ScreenshotCapture:
            # This path intentionally does not call ``controller.snapshot``:
            # hierarchy and foreground reads are unrelated to a visual-only
            # request and can make an otherwise valid screenshot fail.
            return await _capture_screenshot_only(session.require_controller())

        snapshot = await session.run_operation(
            "screenshot",
            capture,
            operation_id=operation_id,
            retry_safe=True,
            mutating=False,
        )
    except MobileUseError as error:
        failure = _failure(error).model_dump(mode="json")
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=json.dumps(failure))],
            structuredContent=failure,
        )
    except Exception:
        failure = _observation_failure("capture the Android screenshot")
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=json.dumps(failure))],
            structuredContent=failure,
        )
    image_data, image_width, image_height = encode_screenshot(
        snapshot.screenshot_png,
        image_format=image_format,
        image_quality=image_quality,
    )
    metadata = ScreenshotResult(
        success=True,
        operation_id=operation_id,
        serial=snapshot.serial,
        session_id=session.session_id,
        generation=session.generation,
        screen_revision=session.screen_revision,
        captured_at=datetime.now(UTC),
        width=snapshot.width,
        height=snapshot.height,
        image_format=image_format,
        image_width=image_width,
        image_height=image_height,
        hierarchy_collected=False,
        foreground_app_collected=False,
        **_image_delivery_metadata("android_screenshot", image_format, image_quality),
    ).model_dump(mode="json")
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(metadata)),
            Image(data=image_data, format=image_format).to_image_content(),
        ],
        structuredContent=metadata,
    )


@mcp.tool(
    name="android_get_ui_elements",
    title="Get Android UI elements",
    annotations=READ_ONLY,
    structured_output=True,
)
async def android_get_ui_elements(
    snapshot_id: Annotated[
        str | None,
        Field(max_length=128, description="Snapshot ID returned by android_snapshot."),
    ] = None,
    offset: Annotated[int, Field(ge=0, description="Zero-based filtered result offset.")] = 0,
    limit: Annotated[
        int,
        Field(ge=1, le=500, description="Maximum matching elements to return."),
    ] = 100,
    query: Annotated[
        str | None,
        Field(max_length=500, description="Case-insensitive substring across semantic fields."),
    ] = None,
    package: Annotated[
        str | None,
        Field(max_length=512, description="Exact case-insensitive Android package filter."),
    ] = None,
    interactive_only: Annotated[
        bool,
        Field(
            description=(
                "After query/package filtering, keep only clickable, focusable, or scrollable "
                "nodes. Keep false when retrieving static text."
            )
        ),
    ] = False,
) -> dict[str, Any]:
    """Filter and page UI nodes from one unchanged cached snapshot.

    The default page size is 100. Query searches text, content description, resource ID, class,
    and package; package is an additional exact filter. Filters are applied before pagination, so
    reset offset to zero when changing them. Omitting snapshot_id captures a new hierarchy with up
    to 2000 characters per node. Actions and android_wait advance the revision; retained IDs
    remain readable until TTL or capacity eviction.
    """

    operation_id = session.new_operation_id()
    try:

        async def read_snapshot(_context: Any) -> tuple[Any, str]:
            if snapshot_id is None:
                observation_context = session.observation_context()
                snapshot = await session.require_controller().snapshot(
                    interactive_only=False,
                    max_elements=None,
                    max_text_length=min(2_000, session.config.snapshot_max_text_length),
                )
                resolved_snapshot_id = session.store_snapshot(
                    snapshot,
                    expected_context=observation_context,
                )
                snapshot = session.get_snapshot(resolved_snapshot_id)
            else:
                snapshot = session.get_snapshot(snapshot_id)
                resolved_snapshot_id = snapshot_id
            return snapshot, resolved_snapshot_id

        snapshot, resolved_snapshot_id = await session.run_operation(
            "get_ui_elements",
            read_snapshot,
            operation_id=operation_id,
            retry_safe=True,
            mutating=False,
        )
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _observation_failure("read Android UI elements")

    elements = snapshot.elements
    if interactive_only:
        elements = [element for element in elements if element.is_interactive()]
    if package:
        normalized_package = package.casefold()
        elements = [
            element
            for element in elements
            if (element.package or "").casefold() == normalized_package
        ]
    if query:
        normalized_query = query.casefold()

        def matches_query(element: UIElement) -> bool:
            values = (
                element.text,
                element.content_description,
                element.resource_id,
                element.class_name,
                element.package,
            )
            return any(normalized_query in (value or "").casefold() for value in values)

        elements = [element for element in elements if matches_query(element)]

    page = elements[offset : offset + limit]
    next_offset = offset + len(page)
    has_more = next_offset < len(elements)
    return {
        "success": True,
        "snapshot_id": resolved_snapshot_id,
        "serial": snapshot.serial,
        "session_id": snapshot.session_id,
        "generation": snapshot.generation,
        "screen_revision": snapshot.screen_revision,
        "captured_at": snapshot.captured_at.isoformat(),
        "width": snapshot.width,
        "height": snapshot.height,
        "warnings": snapshot.warnings,
        "snapshot_total_elements": len(snapshot.elements),
        "total_matches": len(elements),
        "count": len(page),
        "element_count": len(page),
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "truncated": has_more,
        "next_offset": next_offset if has_more else None,
        "query": query,
        "package": package,
        "interactive_only": interactive_only,
        "elements": [element.model_dump(mode="json") for element in page],
    }


@mcp.tool(
    name="android_get_foreground_app",
    title="Get foreground Android app",
    annotations=READ_ONLY,
    structured_output=True,
)
async def android_get_foreground_app() -> dict[str, Any]:
    """Return the package and activity currently in the foreground."""

    operation_id = session.new_operation_id()
    try:
        foreground = await session.run_operation(
            "get_foreground_app",
            lambda _context: session.require_controller().get_foreground_app(),
            operation_id=operation_id,
            retry_safe=True,
            mutating=False,
        )
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _observation_failure("read the foreground Android app")
    return {
        "success": True,
        "operation_id": operation_id,
        "foreground_state": "empty" if foreground.package is None else "occupied",
        "foreground_app": foreground.model_dump(mode="json"),
    }


@mcp.tool(
    name="android_list_apps",
    title="List installed Android apps",
    annotations=READ_ONLY,
    structured_output=True,
)
async def android_list_apps(
    query: Annotated[
        str | None,
        Field(
            max_length=512,
            description=(
                "Case-insensitive substring of the package name or localized launcher display "
                "name/label."
            ),
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(ge=1, le=500, description="Maximum matching Android App records to return."),
    ] = 100,
    offset: Annotated[
        int,
        Field(ge=0, description="Zero-based page offset applied after deterministic sorting."),
    ] = 0,
    include_system: Annotated[
        bool,
        Field(description="Include Android system Apps whose package metadata is available."),
    ] = True,
) -> AppListResult:
    """List deterministic Android App records with stable sorting and paging.

    Query searches package names and localized launcher display names/labels without a model or
    fuzzy resolver.
    ``None`` fields in an App record explicitly mean that Android did not expose that metadata on
    the selected device. android_launch_app still requires an exact package such as
    ``com.taobao.idlefish``.
    """

    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    operation_id = session.new_operation_id()
    try:
        inventory = await session.run_operation(
            "list_apps",
            lambda _context: session.require_controller().list_app_inventory(
                query=query,
                include_system=include_system,
            ),
            operation_id=operation_id,
            retry_safe=True,
            mutating=False,
        )
        apps = sorted(
            inventory.apps,
            key=lambda app: (app.package.casefold(), app.package),
        )
        if query:
            normalized_query = query.casefold()
            apps = [
                app
                for app in apps
                if normalized_query in app.package.casefold()
                or (
                    app.launcher_label is not None
                    and normalized_query in app.launcher_label.casefold()
                )
            ]
        if not include_system:
            apps = [app for app in apps if app.is_third_party is True]
        if not apps and query:
            raise MobileUseError(
                ErrorCode.APP_NOT_FOUND,
                f"No installed Android App matched {query!r}.",
                "Use android_list_apps without a query to inspect exact package names and labels.",
                data={"query": query, "include_system": include_system},
                retryable_override=False,
            )
        if inventory.metadata_status == "unavailable" and apps:
            raise MobileUseError(
                ErrorCode.APP_METADATA_UNAVAILABLE,
                "Android returned packages but App metadata could not be read.",
                "Retry after checking the device connection; unknown fields are not launch proof.",
                data={
                    "metadata_status": inventory.metadata_status,
                    "metadata_errors": inventory.metadata_errors,
                    "apps": [app.model_dump(mode="json") for app in apps[:50]],
                },
                retryable_override=True,
            )
    except MobileUseError as error:
        return cast(
            AppListResult,
            _lifecycle_failure(
                AppListResult,
                operation_id=operation_id,
                error=error,
                state=session.state,
                session_id=session.session_id,
                generation=session.generation,
                device=session.device,
                connected=session.connected,
                data={
                    "query": query,
                    "offset": offset,
                    "limit": limit,
                    "include_system": include_system,
                },
            ),
        )
    except Exception:
        error = MobileUseError(
            ErrorCode.APP_QUERY_FAILED,
            "Failed to list installed Android Apps.",
            "Verify the device is connected and retry android_list_apps.",
            retryable_override=True,
        )
        return cast(
            AppListResult,
            _lifecycle_failure(
                AppListResult,
                operation_id=operation_id,
                error=error,
                state=session.state,
                session_id=session.session_id,
                generation=session.generation,
                device=session.device,
                connected=session.connected,
                data={
                    "query": query,
                    "offset": offset,
                    "limit": limit,
                    "include_system": include_system,
                },
            ),
        )
    page = apps[offset : offset + limit]
    next_offset = offset + len(page) if offset + len(page) < len(apps) else None
    return AppListResult(
        success=True,
        operation_id=operation_id,
        message=f"Found {len(apps)} installed Android App(s).",
        state=session.state,
        session_id=session.session_id,
        generation=session.generation,
        device=session.device,
        connected=session.connected,
        query=query,
        total=len(apps),
        count=len(page),
        offset=offset,
        limit=limit,
        has_more=next_offset is not None,
        next_offset=next_offset,
        metadata_status=inventory.metadata_status,
        apps=page,
        packages=[app.package for app in page],
        warnings=inventory.metadata_errors,
    )


DEFAULT_WAIT_TIMEOUT_SECONDS = 10.0
DEFAULT_WAIT_POLL_INTERVAL_SECONDS = 0.25
MAX_WAIT_POLL_INTERVAL_SECONDS = 5.0
MAX_WAIT_POLLS = 10_000
WaitMatcher = Callable[[ScreenSnapshot], tuple[int, UIElement] | None]


@dataclass(slots=True)
class _WaitState:
    """Bounded, privacy-safe evidence accumulated by one condition wait."""

    submitted_at: float
    operation_id: str
    poll_count: int = 0
    last_snapshot: ScreenSnapshot | None = None
    last_observation: dict[str, Any] | None = None
    last_error_stage: str | None = None
    last_error_code: str | None = None

    @property
    def elapsed_ms(self) -> float:
        return round(max(0.0, (monotonic() - self.submitted_at) * 1_000), 2)

    def observe(self, snapshot: ScreenSnapshot) -> None:
        self.last_snapshot = snapshot
        self.last_observation = {
            "available": True,
            "snapshot_id": snapshot.snapshot_id,
            "session_id": snapshot.session_id,
            "generation": snapshot.generation,
            "screen_revision": snapshot.screen_revision,
            "captured_at": snapshot.captured_at.isoformat(),
            "serial": snapshot.serial,
            "width": snapshot.width,
            "height": snapshot.height,
            "element_count": len(snapshot.elements),
            "warnings": list(snapshot.warnings),
        }

    def record_error(self, stage: str, code: str | None = None) -> None:
        self.last_error_stage = stage
        self.last_error_code = code


class _WaitCallToolResult(CallToolResult):
    """MCP error result that remains mapping-compatible for direct callers."""

    def __getitem__(self, key: str) -> Any:
        if self.structuredContent is None:
            raise KeyError(key)
        return self.structuredContent[key]


def _snapshot_fingerprint(snapshot: ScreenSnapshot) -> tuple[object, ...]:
    """Return content identity while ignoring volatile observation provenance."""

    elements = tuple(
        json.dumps(
            element.model_dump(
                mode="json",
                exclude={"element_ref", "bounds_provenance"},
            ),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for element in snapshot.elements
    )
    return (
        hashlib.sha256(snapshot.screenshot_png).digest(),
        snapshot.width,
        snapshot.height,
        elements,
        snapshot.foreground_app.model_dump_json(),
    )


def _wait_text_match(
    snapshot: ScreenSnapshot,
    text: str,
    text_index: int,
) -> tuple[int, UIElement] | None:
    """Find one case-insensitive text/content-description occurrence."""

    query = text.casefold()
    matches = [
        (index, element)
        for index, element in enumerate(snapshot.elements)
        if query in (element.text or "").casefold()
        or query in (element.content_description or "").casefold()
    ]
    return matches[text_index] if text_index < len(matches) else None


def _wait_element_match(snapshot: ScreenSnapshot, target: Target) -> tuple[int, UIElement] | None:
    """Resolve a presence target against a complete hierarchy.

    Unlike action targeting, a wait observes presence rather than dispatching
    a command. Disabled or bounds-less nodes therefore still satisfy the
    condition when their selectors identify them.
    """

    candidates = list(enumerate(snapshot.elements))

    def intersect(matches: list[tuple[int, UIElement]]) -> None:
        nonlocal candidates
        allowed = {index for index, _element in matches}
        candidates = [item for item in candidates if item[0] in allowed]

    if target.element_ref is not None:
        intersect([item for item in candidates if item[1].element_ref == target.element_ref])
    if target.bounds is not None:
        intersect([item for item in candidates if item[1].bounds == target.bounds])
    if target.resource_id:
        resource_matches = [
            item for item in candidates if item[1].resource_id == target.resource_id
        ]
        selected = (
            resource_matches[target.resource_id_index]
            if target.resource_id_index < len(resource_matches)
            else None
        )
        candidates = [selected] if selected is not None else []
    if target.text:
        query = target.text.casefold()
        text_matches = [
            item
            for item in candidates
            if query
            in {
                (item[1].text or "").casefold(),
                (item[1].content_description or "").casefold(),
            }
        ]
        selected = (
            text_matches[target.text_index]
            if target.text_index < len(text_matches)
            else None
        )
        candidates = [selected] if selected is not None else []
    return candidates[0] if candidates else None


_WAIT_DISCONNECT_CODES = {
    ErrorCode.DEVICE_DISCONNECTED,
    ErrorCode.DEVICE_NOT_FOUND,
    ErrorCode.DEVICE_OFFLINE,
    ErrorCode.DEVICE_UNAUTHORIZED,
    ErrorCode.NOT_CONNECTED,
}


def _wait_timeout_error(
    *,
    condition: Literal["text", "element", "ui_change"],
    timeout_seconds: float,
    poll_interval_seconds: float,
    state: _WaitState,
) -> MobileUseError:
    """Build one timeout carrying only bounded wait evidence."""

    details: dict[str, Any] = {
        "operation_id": state.operation_id,
        "condition": condition,
        "poll_count": state.poll_count,
        "elapsed_ms": state.elapsed_ms,
        "timeout_seconds": timeout_seconds,
        "poll_interval_seconds": poll_interval_seconds,
        "last_observation": state.last_observation,
        "last_safe_observation": state.last_observation,
        "last_error_stage": state.last_error_stage,
        "last_error_code": state.last_error_code,
    }
    return MobileUseError(
        ErrorCode.TIMEOUT,
        f"Android wait for {condition} condition timed out.",
        "Call android_snapshot to inspect the current screen, then retry with a bounded wait.",
        data=details,
        retryable_override=True,
    )


def _wait_error_with_evidence(
    error: MobileUseError,
    *,
    condition: Literal["text", "element", "ui_change"],
    timeout_seconds: float,
    poll_interval_seconds: float,
    state: _WaitState,
) -> MobileUseError:
    """Attach wait diagnostics to an early device/session failure."""

    details = dict(error.data)
    details.update(
        {
            "operation_id": state.operation_id,
            "condition": condition,
            "poll_count": state.poll_count,
            "elapsed_ms": state.elapsed_ms,
            "timeout_seconds": timeout_seconds,
            "poll_interval_seconds": poll_interval_seconds,
            "last_observation": state.last_observation,
            "last_safe_observation": state.last_observation,
            "last_error_stage": state.last_error_stage,
            "last_error_code": state.last_error_code,
        }
    )
    return MobileUseError(
        error.code,
        error.message,
        error.suggestion,
        data=details,
        retryable_override=error.retryable,
    )


def _wait_failure(
    error: MobileUseError,
    *,
    operation_id: str,
    condition: Literal["text", "element", "ui_change"],
    timeout_seconds: float,
    poll_interval_seconds: float,
    baseline_snapshot_id: str | None = None,
    baseline_screen_revision: int | None = None,
) -> _WaitCallToolResult:
    """Serialize a wait failure and mark it as an MCP business error."""

    details = dict(error.data)
    last_observation = details.get("last_observation")
    last_observation_data = (
        cast(dict[str, Any], last_observation) if isinstance(last_observation, dict) else None
    )
    payload = WaitResult(
        success=False,
        operation_id=operation_id,
        condition=condition,
        message=error.message,
        poll_count=int(details.get("poll_count", 0)),
        polls=int(details.get("poll_count", 0)),
        elapsed_ms=float(details.get("elapsed_ms", 0.0)),
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        snapshot_id=(
            str(last_observation_data["snapshot_id"])
            if last_observation_data and last_observation_data.get("snapshot_id") is not None
            else None
        ),
        session_id=(
            str(last_observation_data["session_id"])
            if last_observation_data and last_observation_data.get("session_id") is not None
            else None
        ),
        generation=(
            int(last_observation_data["generation"])
            if last_observation_data and last_observation_data.get("generation") is not None
            else None
        ),
        screen_revision=(
            int(last_observation_data.get("screen_revision", 0))
            if last_observation_data
            else 0
        ),
        baseline_snapshot_id=baseline_snapshot_id,
        baseline_screen_revision=baseline_screen_revision,
        last_observation=last_observation_data,
        last_safe_observation=last_observation_data,
        last_error_stage=(
            str(details["last_error_stage"])
            if details.get("last_error_stage") is not None
            else None
        ),
        last_error_code=(
            str(details["last_error_code"])
            if details.get("last_error_code") is not None
            else None
        ),
        error_code=error.code.value,
        suggestion=error.suggestion,
        data=details,
    ).model_dump(mode="json")
    return _WaitCallToolResult(
        isError=True,
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structuredContent=payload,
    )


async def _call_condition_wait(
    *,
    condition: Literal["text", "element", "ui_change"],
    timeout_seconds: float,
    poll_interval_seconds: float,
    matcher: WaitMatcher | None,
    baseline_snapshot_id: str | None = None,
    baseline_screen_revision: int | None = None,
) -> WaitResult:
    """Invoke one condition wait and map business failures to MCP errors."""

    operation_id = session.new_operation_id()
    try:
        return await _run_condition_wait(
            condition=condition,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            matcher=matcher,
            operation_id=operation_id,
            baseline_snapshot_id=baseline_snapshot_id,
            baseline_screen_revision=baseline_screen_revision,
        )
    except MobileUseError as error:
        return cast(
            WaitResult,
            _wait_failure(
                error,
                operation_id=str(error.data.get("operation_id", operation_id)),
                condition=condition,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                baseline_snapshot_id=baseline_snapshot_id,
                baseline_screen_revision=baseline_screen_revision,
            ),
        )
    except Exception:
        error = MobileUseError(
            ErrorCode.OPERATION_FAILED,
            f"Failed while waiting for Android {condition}.",
            "Verify the device is connected and responsive, then retry the bounded wait.",
            data={"operation_id": operation_id},
        )
        return cast(
            WaitResult,
            _wait_failure(
                error,
                operation_id=operation_id,
                condition=condition,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                baseline_snapshot_id=baseline_snapshot_id,
                baseline_screen_revision=baseline_screen_revision,
            ),
        )


async def _run_condition_wait(
    *,
    condition: Literal["text", "element", "ui_change"],
    timeout_seconds: float,
    poll_interval_seconds: float,
    matcher: WaitMatcher | None,
    operation_id: str | None = None,
    baseline_snapshot_id: str | None = None,
    baseline_screen_revision: int | None = None,
) -> WaitResult:
    """Poll complete Android observations through the DeviceSession gateway."""

    resolved_operation_id = operation_id or session.new_operation_id()
    state = _WaitState(submitted_at=monotonic(), operation_id=resolved_operation_id)

    async def wait(context: Any) -> WaitResult:
        baseline: ScreenSnapshot | None = None
        if baseline_snapshot_id is not None:
            context.checkpoint("baseline")
            baseline = session.get_snapshot(baseline_snapshot_id, operation=context)
            effective_baseline_revision = (
                baseline_screen_revision
                if baseline_screen_revision is not None
                else baseline.screen_revision
            )
        else:
            effective_baseline_revision = baseline_screen_revision

        while True:
            state.poll_count += 1
            try:
                context.checkpoint("poll")
                if state.poll_count > MAX_WAIT_POLLS:
                    raise _wait_timeout_error(
                        condition=condition,
                        timeout_seconds=timeout_seconds,
                        poll_interval_seconds=poll_interval_seconds,
                        state=state,
                    )
                if getattr(session, "disconnect_requested", False) is True:
                    raise MobileUseError(
                        ErrorCode.DEVICE_DISCONNECTED,
                        "The Android session is closing while the condition wait is running.",
                        "Wait for android_disconnect to finish, then reconnect before retrying.",
                    )
                controller = session.require_controller()
                observation_context = session.observation_context()
                context.checkpoint("observation")
                snapshot = await session.capture_complete_snapshot(
                    controller,
                    min(2_000, session.config.snapshot_max_text_length),
                )
                context.checkpoint("observation")
                if session.observation_context() != observation_context:
                    raise MobileUseError(
                        ErrorCode.SNAPSHOT_STALE,
                        "The Android observation finished after the screen changed.",
                        "Discard this observation and retry the condition wait.",
                    )
                state.observe(snapshot)

                if condition == "ui_change":
                    changed = (
                        baseline is not None
                        and _snapshot_fingerprint(snapshot) != _snapshot_fingerprint(baseline)
                    ) or (
                        effective_baseline_revision is not None
                        and observation_context.screen_revision != effective_baseline_revision
                    )
                    match = True if changed else None
                else:
                    if matcher is None:
                        raise RuntimeError("element wait matcher is not configured")
                    match = matcher(snapshot)
                if match:
                    if getattr(session, "disconnect_requested", False) is True:
                        raise MobileUseError(
                            ErrorCode.DEVICE_DISCONNECTED,
                            "The Android session is closing while the condition wait is running.",
                            (
                                "Wait for android_disconnect to finish, then reconnect before "
                                "retrying."
                            ),
                        )
                    context.checkpoint("snapshot_commit")
                    snapshot_id = session.store_snapshot(
                        snapshot,
                        operation=context,
                        expected_context=observation_context,
                    )
                    final = session.get_snapshot(snapshot_id, operation=context)
                    state.observe(final)
                    matched_element = None
                    # A text wait's query may itself be sensitive.  Return
                    # the immutable snapshot provenance but do not echo the
                    # matching node (which could contain the queried value).
                    if condition != "text" and isinstance(match, tuple):
                        matched_index = match[0]
                        if 0 <= matched_index < len(final.elements):
                            matched_element = final.elements[matched_index]
                    return WaitResult(
                        success=True,
                        operation_id=resolved_operation_id,
                        condition=condition,
                        message=f"Android {condition} condition satisfied.",
                        poll_count=state.poll_count,
                        polls=state.poll_count,
                        elapsed_ms=context.elapsed_ms,
                        timeout_seconds=timeout_seconds,
                        poll_interval_seconds=poll_interval_seconds,
                        snapshot_id=final.snapshot_id,
                        session_id=final.session_id,
                        generation=final.generation,
                        screen_revision=final.screen_revision,
                        captured_at=final.captured_at,
                        matched_element=matched_element,
                        baseline_snapshot_id=baseline_snapshot_id,
                        baseline_screen_revision=effective_baseline_revision,
                        last_observation=state.last_observation,
                        last_safe_observation=state.last_observation,
                        data={
                            "condition_met": True,
                            "snapshot_id": final.snapshot_id,
                            "session_id": final.session_id,
                            "generation": final.generation,
                            "screen_revision": final.screen_revision,
                            "captured_at": final.captured_at.isoformat(),
                        },
                    )
                state.record_error("condition")
            except asyncio.CancelledError:
                raise
            except MobileUseError as error:
                if error.code in _WAIT_DISCONNECT_CODES:
                    state.record_error("controller", error.code.value)
                    raise _wait_error_with_evidence(
                        error,
                        condition=condition,
                        timeout_seconds=timeout_seconds,
                        poll_interval_seconds=poll_interval_seconds,
                        state=state,
                    ) from error
                if error.code == ErrorCode.CANCELLED:
                    raise
                if (
                    error.code == ErrorCode.TIMEOUT
                    and error.data.get("condition") == condition
                    and error.data.get("poll_count") == state.poll_count
                ):
                    raise
                if error.code == ErrorCode.TIMEOUT and context.remaining_seconds() <= 0:
                    raise _wait_timeout_error(
                        condition=condition,
                        timeout_seconds=timeout_seconds,
                        poll_interval_seconds=poll_interval_seconds,
                        state=state,
                    ) from error
                state.record_error("observation", error.code.value)
            except Exception:
                # Adapter errors are intentionally reduced to one stable
                # stage/code and retried until the operation deadline.
                state.record_error("observation", ErrorCode.OPERATION_FAILED.value)

            if context.remaining_seconds() <= 0:
                raise _wait_timeout_error(
                    condition=condition,
                    timeout_seconds=timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                    state=state,
                )
            # Leave a small scheduling margin so the callback can publish the
            # rich wait diagnostics before the gateway's outer deadline wins.
            margin = min(0.01, timeout_seconds / 4)
            remaining = context.remaining_seconds()
            if remaining <= margin:
                raise _wait_timeout_error(
                    condition=condition,
                    timeout_seconds=timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                    state=state,
                )
            await asyncio.sleep(min(poll_interval_seconds, remaining - margin))

    try:
        return await session.run_operation(
            f"wait_for_{condition}",
            wait,
            operation_id=resolved_operation_id,
            timeout_seconds=timeout_seconds,
            retry_safe=True,
            mutating=False,
        )
    except MobileUseError as error:
        # A blocking adapter may still be cancelled by the gateway's outer
        # deadline before the callback can assemble its own diagnostics.
        # Preserve the same wait-level evidence in that race.
        if error.code == ErrorCode.TIMEOUT and error.data.get("condition") != condition:
            timeout_error = _wait_timeout_error(
                condition=condition,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                state=state,
            )
            gateway_stage = error.data.get("stage")
            if isinstance(gateway_stage, str):
                timeout_error.data["stage"] = gateway_stage
                timeout_error.data["last_error_stage"] = gateway_stage
            raise timeout_error from error
        raise


def _observation_failure(action: str) -> dict[str, Any]:
    return OperationResult(
        success=False,
        error_code="OPERATION_FAILED",
        message=f"Failed to {action}.",
        suggestion="Verify the device is connected, unlocked, and responsive, then retry.",
    ).model_dump(mode="json")


def _unexpected_failure(action: str) -> dict[str, Any]:
    return OperationResult(
        success=False,
        error_code="OPERATION_FAILED",
        message=f"Android {action} failed.",
        suggestion="Call android_snapshot to inspect the current device state, then retry.",
    ).model_dump(mode="json")


def _configured_method(instance: Any, name: str) -> Any | None:
    """Find an explicitly implemented/injected method without Mock auto-attrs."""

    method = getattr(instance, name, None)
    if not callable(method):
        return None
    owner = cast(type[Any], type(instance))
    if (
        getattr(owner, name, None) is not None
        or name in getattr(instance, "__dict__", {})
        or hasattr(method, "assert_awaited")
    ):
        return method
    return None


def _target_requires_validation(target: Target) -> bool:
    """Identify targets that cannot safely bypass the session resolver."""

    return any(
        value is not None
        for value in (
            target.element_ref,
            target.bounds_provenance,
            target.snapshot_id,
            target.resource_id,
            target.text,
        )
    )


async def _authorize_action(
    context: Any,
    controller: Any,
    *,
    action: str,
    target_package: str | None = None,
    target_package_required: bool = False,
    target_surface: str | None = None,
) -> Any:
    """Run the session-owned package policy gate before an Android command."""

    checker = getattr(session, "check_package_policy", None)
    if not callable(checker):
        # A real DeviceSession always exposes this method. A session that
        # advertises a policy but omits the checker is unsafe and therefore
        # fails closed instead of letting a compatibility adapter bypass it.
        configured_policy = getattr(session, "policy", None)
        if configured_policy is not None:
            raise MobileUseError(
                ErrorCode.PACKAGE_POLICY_FOREGROUND_UNAVAILABLE,
                "The Android package policy boundary is unavailable.",
                "Reconnect through the built-in DeviceSession before retrying the action.",
                data={
                    "stage": "package_policy",
                    "action": action,
                    "reason": "policy_checker_unavailable",
                },
                retryable_override=False,
            )
        return None
    result = checker(
        action=action,
        controller=controller,
        target_package=target_package,
        target_package_required=target_package_required,
        target_surface=target_surface,
        operation=context,
    )
    if isawaitable(result):
        return await result
    return result


def _resolved_target_package(snapshot: ScreenSnapshot, resolved: Any) -> str | None:
    """Recover the package owning a resolved target for policy enforcement."""

    element = getattr(resolved, "element", None)
    package = getattr(element, "package", None)
    if isinstance(package, str) and package:
        return package
    bounds = getattr(resolved, "bounds", None)
    if bounds is None:
        return None
    packages = {
        element.package
        for element in snapshot.elements
        if element.bounds == bounds and element.package
    }
    return next(iter(packages)) if len(packages) == 1 else None


def _safe_input_data(value: Any, *, secret: str | None = None) -> dict[str, object]:
    """Normalize an input outcome without allowing payload values to escape."""

    def unknown(method: str) -> dict[str, object]:
        return InputOutcome(
            method=method,
            command_status="sent",
            effect_status="unverified",
            restoration_status="unknown",
            focus_status="unverified",
            verification_available=False,
            requires_observation=True,
            fallback_used=method == "adb",
        ).to_data()

    if isinstance(value, InputOutcome):
        data = value.to_data()
        method = data.get("method")
        data["method"] = (
            method if isinstance(method, str) and method in {"uiautomator2", "adb"} else "unknown"
        )
        return cast(dict[str, object], _redact_exact(data, secret))
    if isinstance(value, str):
        method = value if value in {"uiautomator2", "adb"} else "unknown"
        return cast(dict[str, object], _redact_exact(unknown(method), secret))
    if isinstance(value, dict):
        sensitive_keys = {
            "text",
            "value",
            "input",
            "payload",
            "plaintext",
            "secret",
            "password",
        }

        def redact(item: Any) -> Any:
            if isinstance(item, BaseModel):
                return redact(item.model_dump(mode="json"))
            if isinstance(item, dict):
                mapping = cast(dict[Any, Any], item)
                return {
                    str(key): redact(nested)
                    for key, nested in mapping.items()
                    if str(key).casefold() not in sensitive_keys
                }
            if isinstance(item, list):
                items = cast(list[Any], item)
                return [redact(nested) for nested in items[:50]]
            if isinstance(item, tuple):
                items = cast(tuple[Any, ...], item)
                return tuple(redact(nested) for nested in items[:50])
            return _redact_secret(item, secret)

        mapping = cast(dict[Any, Any], value)
        return {
            str(key): redact(item)
            for key, item in mapping.items()
            if str(key).casefold() not in sensitive_keys
        }
    return cast(dict[str, object], _redact_secret(unknown("unknown"), secret))


async def _resolve_and_focus_target(
    context: Any,
    target: Target,
) -> tuple[dict[str, object], UIElement | None]:
    """Resolve, dispatch, and re-check one input target through the session gateway."""

    # Keep the callback seam source-compatible with lightweight injected
    # controllers; the gateway context is passed explicitly to target
    # resolution below and is also available through the operation contextvar.
    controller = session.require_controller()
    resolver = _configured_method(controller, "tap_resolved")
    verifier = _configured_method(controller, "verify_focused_element")
    if resolver is None or verifier is None:
        raise MobileUseError(
            ErrorCode.UNSUPPORTED,
            "The active Android controller cannot perform stale-safe input focus.",
            "Reconnect with the built-in Android controller and retry the action.",
            data={"stage": "focus", "focus_status": "unverified"},
        )

    snapshot, resolved = await session.resolve_target(
        target,
        operation=context,
        controller=controller,
    )
    target_package = _resolved_target_package(snapshot, resolved)
    expected_element = resolved.element or UIElement(bounds=resolved.bounds)
    if expected_element.package is None and target_package is not None:
        expected_element = expected_element.model_copy(update={"package": target_package})
    # Invalidate the copied target capability before dispatch. The focus
    # re-check reads a fresh hierarchy directly and therefore does not reuse
    # the stale target through the session resolver.
    session.advance_screen_revision(operation=context)
    context.checkpoint("focus_dispatch")
    await _authorize_action(
        context,
        controller,
        action="focus",
        target_package=target_package,
        target_package_required=True,
    )
    context.checkpoint("focus_dispatch")
    dispatch_result = resolver(resolved)
    if isawaitable(dispatch_result):
        await dispatch_result
    context.checkpoint("focus_verify")
    verification = verifier(expected_element)
    if isawaitable(verification):
        verification = await verification
    if verification is False:
        raise MobileUseError(
            ErrorCode.INPUT_FOCUS_FAILED,
            "The Android input field was not focused at execution time.",
            "Call android_snapshot, verify the input element, and try again.",
            data={"stage": "focus", "focus_status": "unverified"},
            retryable_override=False,
        )
    # Keep only the stable, scalar target audit fields. Adapter-specific
    # payloads are deliberately not propagated because they may contain a
    # field's plaintext or an opaque object whose serializer is not secret-safe.
    details: dict[str, object] = {}
    x, y = resolved.bounds.center
    details.update(
        {
            "x": x,
            "y": y,
            "selector": resolved.selector,
            "matched_selector": resolved.matched_selector or resolved.selector,
            "attempts": [attempt.model_dump(mode="json") for attempt in resolved.attempts],
            "focus_status": "verified",
            "focus_verification_available": True,
        }
    )
    return details, expected_element


async def _resolve_and_dispatch_target(
    context: Any,
    target: Target,
    *,
    long_press: bool = False,
    duration_ms: int = 1_000,
) -> dict[str, object]:
    """Validate a target before advancing the revision and dispatching input."""

    controller = session.require_controller()
    tap_resolved = _configured_method(
        controller,
        "long_press_resolved" if long_press else "tap_resolved",
    )
    snapshot_reader = _configured_method(controller, "snapshot")
    resolved = None
    target_package: str | None = None
    should_validate = (
        tap_resolved is not None
        or snapshot_reader is not None
        or session.session_id is not None
        or _target_requires_validation(target)
    )
    if should_validate:
        snapshot, resolved = await session.resolve_target(
            target,
            operation=context,
            controller=controller,
        )
        target_package = _resolved_target_package(snapshot, resolved)
    # Invalidate copied bounds/ref capabilities only after all validation has
    # completed; a stale/conflicting target therefore sends no device command.
    session.advance_screen_revision(operation=context)
    context.checkpoint("dispatch")
    await _authorize_action(
        context,
        controller,
        action="long_press" if long_press else "tap",
        target_package=target_package,
        target_package_required=True,
    )
    context.checkpoint("dispatch")
    if tap_resolved is not None and resolved is not None:
        result = tap_resolved(resolved, duration_ms) if long_press else tap_resolved(resolved)
        if isawaitable(result):
            result = await result
        if isinstance(result, dict):
            # Keep the resolver's audit trail authoritative even when an
            # injected adapter returns only an execution-specific payload.
            # Built-in controllers already include these fields, while the
            # defaults make the MCP contract stable for lightweight fakes.
            details = dict(cast(dict[str, object], result))
            x, y = resolved.bounds.center
            details.setdefault("x", x)
            details.setdefault("y", y)
            details["selector"] = resolved.selector
            details["matched_selector"] = resolved.matched_selector or resolved.selector
            details["attempts"] = [attempt.model_dump(mode="json") for attempt in resolved.attempts]
            if long_press:
                details.setdefault("duration_ms", duration_ms)
            return details
        x, y = resolved.bounds.center
        fallback: dict[str, object] = {
            "x": x,
            "y": y,
            "selector": resolved.selector,
            "matched_selector": resolved.matched_selector or resolved.selector,
            "attempts": [attempt.model_dump(mode="json") for attempt in resolved.attempts],
        }
        if long_press:
            fallback["duration_ms"] = duration_ms
        return fallback
    if should_validate:
        raise MobileUseError(
            ErrorCode.UNSUPPORTED,
            "The active Android controller cannot dispatch a resolved target.",
            "Reconnect with the built-in Android controller and retry the action.",
        )
    result = controller.long_press(target, duration_ms) if long_press else controller.tap(target)
    if isawaitable(result):
        result = await result
    return result


@mcp.tool(
    name="android_tap",
    title="Tap Android UI target",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_tap(target: Target) -> dict[str, Any]:
    """Tap using an opaque ref, provenance-bound bounds, or semantic fallbacks.

    A successful call means the input command was sent, not that the expected UI state appeared.
    Observe again afterward; wait briefly first when the page animates or loads asynchronously.
    """

    operation_id = session.new_operation_id()
    try:

        async def act(_context: Any) -> Any:
            return await _resolve_and_dispatch_target(_context, target)

        details = await session.run_operation(
            "tap",
            act,
            operation_id=operation_id,
            mutating=True,
        )
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _unexpected_failure("tap")
    return OperationResult(
        success=True,
        message=f"Tapped using {details['selector']}.",
        data=details,
    ).model_dump(mode="json")


@mcp.tool(
    name="android_long_press",
    title="Long press Android UI target",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_long_press(
    target: Target,
    duration_ms: Annotated[
        int,
        Field(ge=100, le=10_000, description="Press duration in milliseconds."),
    ] = 1_000,
) -> dict[str, Any]:
    """Long press an opaque ref, provenance-bound bounds, or semantic fallbacks.

    Observe again afterward to verify the resulting UI state.
    """

    operation_id = session.new_operation_id()
    try:

        async def act(_context: Any) -> Any:
            return await _resolve_and_dispatch_target(
                _context,
                target,
                long_press=True,
                duration_ms=duration_ms,
            )

        details = await session.run_operation(
            "long_press",
            act,
            operation_id=operation_id,
            mutating=True,
        )
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _unexpected_failure("long press")
    return OperationResult(
        success=True,
        message=f"Long pressed using {details['selector']}.",
        data=details,
    ).model_dump(mode="json")


@mcp.tool(
    name="android_swipe",
    title="Swipe Android screen",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_swipe(
    start_x: Annotated[
        int | float | None,
        Field(default=None, ge=0, description="Start X in original device pixels."),
    ] = None,
    start_y: Annotated[
        int | float | None,
        Field(default=None, ge=0, description="Start Y in original device pixels."),
    ] = None,
    end_x: Annotated[
        int | float | None,
        Field(default=None, ge=0, description="End X in original device pixels."),
    ] = None,
    end_y: Annotated[
        int | float | None,
        Field(default=None, ge=0, description="End Y in original device pixels."),
    ] = None,
    duration_ms: Annotated[
        int,
        Field(ge=1, le=10_000, description="Swipe duration in milliseconds."),
    ] = 400,
    coordinate_mode: Annotated[
        Literal["pixels", "percentage", "percent"] | None,
        Field(
            default=None,
            description=(
                "Optional explicit mode for the four flat coordinates. Use percentage/percent "
                "to interpret them as normalized values; omitted mode requires integer pixels."
            ),
        ),
    ] = None,
    percentage: Annotated[
        SwipePercentage | None,
        Field(
            default=None,
            description=(
                "Normalized coordinates in [0, 1]. Use this instead of all four pixel "
                "coordinates; do not mix the two modes."
            ),
        ),
    ] = None,
    start_x_percent: Annotated[
        float | None,
        Field(default=None, ge=0, le=1, description="Normalized start X in [0, 1]."),
    ] = None,
    start_y_percent: Annotated[
        float | None,
        Field(default=None, ge=0, le=1, description="Normalized start Y in [0, 1]."),
    ] = None,
    end_x_percent: Annotated[
        float | None,
        Field(default=None, ge=0, le=1, description="Normalized end X in [0, 1]."),
    ] = None,
    end_y_percent: Annotated[
        float | None,
        Field(default=None, ge=0, le=1, description="Normalized end Y in [0, 1]."),
    ] = None,
) -> dict[str, Any]:
    """Swipe in pixels or one validated normalized-coordinate mode.

    Screenshot previews may be visually scaled by the host. Pixel mode uses the reported native
    width and height. Percentage mode converts each value against the current native dimensions;
    all four values are required and pixel/percentage fields cannot be mixed. For clients that
    prefer flat coordinates, ``coordinate_mode="percentage"`` (or ``"percent"``) interprets
    the four coordinate values as normalized values.
    """

    pixel_values = (start_x, start_y, end_x, end_y)
    flat_percentage_values = (
        start_x_percent,
        start_y_percent,
        end_x_percent,
        end_y_percent,
    )
    has_pixels = any(value is not None for value in pixel_values)
    has_flat_percentage = any(value is not None for value in flat_percentage_values)
    explicit_percentage = coordinate_mode in {"percentage", "percent"}
    explicit_pixels = coordinate_mode == "pixels"
    normalized: SwipePercentage | None = None
    if coordinate_mode not in {None, "pixels", "percentage", "percent"}:
        return _failure(
            MobileUseError(
                ErrorCode.INVALID_COORDINATES,
                "coordinate_mode must be pixels or percentage.",
                'Use coordinate_mode="pixels" or coordinate_mode="percentage".',
            )
        ).model_dump(mode="json")
    if explicit_percentage:
        if (
            percentage is not None
            or has_flat_percentage
            or not all(value is not None for value in pixel_values)
        ):
            return _failure(
                MobileUseError(
                    ErrorCode.INVALID_COORDINATES,
                    "Flat percentage mode requires all four coordinate values and no other mode.",
                    'Pass four start/end values with coordinate_mode="percentage" only.',
                )
            ).model_dump(mode="json")
        try:
            normalized = SwipePercentage(
                start_x=float(cast(float, start_x)),
                start_y=float(cast(float, start_y)),
                end_x=float(cast(float, end_x)),
                end_y=float(cast(float, end_y)),
            )
        except (TypeError, ValueError):
            return _failure(
                MobileUseError(
                    ErrorCode.INVALID_COORDINATES,
                    "Percentage coordinates must be numbers in the [0, 1] range.",
                    "Use four normalized values between 0 and 1.",
                )
            ).model_dump(mode="json")
        has_pixels = False
    elif explicit_pixels and (percentage is not None or has_flat_percentage):
        return _failure(
            MobileUseError(
                ErrorCode.INVALID_COORDINATES,
                "Pixel mode cannot be combined with percentage coordinates.",
                "Provide only the four integer pixel coordinates.",
            )
        ).model_dump(mode="json")
    if percentage is not None and has_flat_percentage:
        return _failure(
            MobileUseError(
                ErrorCode.INVALID_COORDINATES,
                "Use either percentage or the flat percentage fields, not both.",
                "Provide exactly one complete swipe coordinate mode.",
            )
        ).model_dump(mode="json")
    if (
        (has_pixels and (percentage is not None or has_flat_percentage))
        or (has_pixels and not all(value is not None for value in pixel_values))
        or (has_pixels and any(value is not None and value < 0 for value in pixel_values))
        or (
            has_pixels
            and not all(
                isinstance(value, int) and not isinstance(value, bool) for value in pixel_values
            )
        )
    ):
        return _failure(
            MobileUseError(
                ErrorCode.INVALID_COORDINATES,
                "Swipe coordinates must use all four pixel values or one percentage mode.",
                "Provide start_x/start_y/end_x/end_y, or percentage with all four "
                "normalized values.",
            )
        ).model_dump(mode="json")
    if not explicit_percentage:
        percentage_value: Any = percentage
        if percentage_value is not None and not isinstance(percentage_value, SwipePercentage):
            try:
                normalized = SwipePercentage.model_validate(percentage_value)
            except (TypeError, ValueError):
                return _failure(
                    MobileUseError(
                        ErrorCode.INVALID_COORDINATES,
                        "Percentage coordinates must contain four values in the [0, 1] range.",
                        "Provide percentage.start_x, start_y, end_x, and end_y between 0 and 1.",
                    )
                ).model_dump(mode="json")
        elif percentage_value is not None:
            normalized = percentage_value
        if has_flat_percentage:
            if not all(value is not None for value in flat_percentage_values):
                return _failure(
                    MobileUseError(
                        ErrorCode.INVALID_COORDINATES,
                        "All four flat percentage coordinates are required.",
                        "Provide start_x_percent, start_y_percent, end_x_percent, and "
                        "end_y_percent.",
                    )
                ).model_dump(mode="json")
            try:
                normalized = SwipePercentage(
                    start_x=cast(float, start_x_percent),
                    start_y=cast(float, start_y_percent),
                    end_x=cast(float, end_x_percent),
                    end_y=cast(float, end_y_percent),
                )
            except (TypeError, ValueError):
                return _failure(
                    MobileUseError(
                        ErrorCode.INVALID_COORDINATES,
                        "Percentage coordinates must be numbers in the [0, 1] range.",
                        "Use four normalized values between 0 and 1.",
                    )
                ).model_dump(mode="json")
    if not has_pixels and normalized is None:
        return _failure(
            MobileUseError(
                ErrorCode.INVALID_COORDINATES,
                "Swipe coordinates were not provided.",
                "Provide all four pixel coordinates or one complete percentage mode.",
            )
        ).model_dump(mode="json")

    operation_id = session.new_operation_id()
    try:

        async def act(context: Any) -> dict[str, object]:
            controller = session.require_controller()
            if normalized is not None:
                percentage_method = _configured_method(controller, "swipe_percentage")
                if percentage_method is not None:
                    await _authorize_action(context, controller, action="swipe")
                    session.advance_screen_revision(operation=context)
                    context.checkpoint("dispatch")
                    result = percentage_method(normalized, duration_ms)
                    if isawaitable(result):
                        result = await result
                    if isinstance(result, dict):
                        return cast(dict[str, object], result)
                # An injected controller can still support percentage mode by
                # exposing only a complete snapshot and its existing pixel
                # swipe method.
                current = await session.capture_complete_snapshot(
                    controller,
                    min(2_000, session.config.snapshot_max_text_length),
                )

                actual = (
                    normalized_coordinate_to_pixel(normalized.start_x, current.width),
                    normalized_coordinate_to_pixel(normalized.start_y, current.height),
                    normalized_coordinate_to_pixel(normalized.end_x, current.width),
                    normalized_coordinate_to_pixel(normalized.end_y, current.height),
                )
                await _authorize_action(context, controller, action="swipe")
                session.advance_screen_revision(operation=context)
                context.checkpoint("dispatch")
                result = controller.swipe(*actual, duration_ms)
            else:
                assert all(value is not None for value in pixel_values)
                actual = cast(tuple[int, int, int, int], pixel_values)
                await _authorize_action(context, controller, action="swipe")
                session.advance_screen_revision(operation=context)
                context.checkpoint("dispatch")
                result = controller.swipe(*actual, duration_ms)
            if isawaitable(result):
                result = await result
            result_value: Any = result
            if isinstance(result_value, dict):
                return cast(dict[str, object], result_value)
            return {
                "start": {"x": actual[0], "y": actual[1]},
                "end": {"x": actual[2], "y": actual[3]},
                "duration_ms": duration_ms,
            }

        details = await session.run_operation(
            "swipe",
            act,
            operation_id=operation_id,
            mutating=True,
        )
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _unexpected_failure("swipe")
    return OperationResult(
        success=True,
        message=(
            "Swiped using normalized coordinates converted to native pixels."
            if normalized is not None
            else "Swiped using native pixel coordinates."
        ),
        data={
            **details,
            "mode": "percentage" if normalized is not None else "pixels",
            "duration_ms": duration_ms,
        },
    ).model_dump(mode="json")


@mcp.tool(
    name="android_type_text",
    title="Type text on Android",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_type_text(
    text: Annotated[
        str,
        Field(
            min_length=1,
            max_length=10_000,
            description="Text to enter; successful responses intentionally do not echo it.",
        ),
    ],
    target: Annotated[
        Target | None,
        Field(
            description=(
                "Optional input target to tap first. Without it, type into the currently focused "
                "field. Bounds-only targets cannot verify focus as reliably as resource_id/text."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Focus and type while reporting command/effect/IME stages separately.

    The text is used only inside the device operation and in-memory effect
    comparison. It is never copied into result data, diagnostics, or logs.
    """

    operation_id = session.new_operation_id()
    try:

        async def act(_context: Any) -> tuple[Any, dict[str, object] | None]:
            controller = session.require_controller()
            target_details: dict[str, object] | None = None
            if (
                target is not None
                and session.session_id is not None
                and _configured_method(controller, "tap_resolved") is not None
                and _configured_method(controller, "verify_focused_element") is not None
            ):
                target_details, expected_element = await _resolve_and_focus_target(_context, target)
                session.advance_screen_revision(operation=_context)
                target_package = (
                    expected_element.package if expected_element is not None else None
                )
                await _authorize_action(
                    _context,
                    controller,
                    action="type_text",
                    target_package=target_package,
                    target_package_required=True,
                )
                detailed = _configured_method(controller, "type_text_detailed")
                if detailed is not None:
                    result = detailed(
                        text,
                        expected_element=expected_element,
                        focus_status="verified",
                    )
                else:
                    result = controller.type_text(text, None)
            else:
                await _authorize_action(
                    _context,
                    controller,
                    action="type_text",
                    target_package_required=target is not None,
                )
                session.clear_snapshot()
                result = controller.type_text(text, target)
            if isawaitable(result):
                result = await result
            return result, target_details

        method, target_details = await session.run_operation(
            "type_text",
            act,
            operation_id=operation_id,
            mutating=True,
        )
    except MobileUseError as error:
        return _failure(error, secret=text).model_dump(mode="json")
    except Exception:
        return _unexpected_failure("text input")
    data: dict[str, Any] = {
        **_safe_input_data(method, secret=text),
        "character_count": len(text),
    }
    if target_details is not None:
        safe_target_details = _redact_secret(target_details, text)
        data["target"] = safe_target_details
        for key in ("selector", "matched_selector", "attempts"):
            if key in target_details:
                data[key] = safe_target_details[key]
    return OperationResult(
        success=True,
        message=(
            "Text input command completed; observe the field to verify the effect."
            if not data.get("effect_verified", False)
            else "Text was entered and verified in the focused field."
        ),
        data=data,
    ).model_dump(mode="json")


@mcp.tool(
    name="android_clear_text",
    title="Clear focused Android text",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_clear_text(
    max_characters: Annotated[
        int,
        Field(
            ge=1,
            le=2_000,
            description="Maximum delete key presses used only by the ADB fallback.",
        ),
    ] = 100,
    target: Annotated[
        Target | None,
        Field(
            description=(
                "Optional input target to tap first. Without it, clear the currently focused field."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Focus and clear text with bounded attempts and an explicit empty check."""

    bounded_max_characters = max(1, min(max_characters, 2_000))
    operation_id = session.new_operation_id()
    try:

        async def act(_context: Any) -> tuple[Any, dict[str, object] | None]:
            controller = session.require_controller()
            target_details: dict[str, object] | None = None
            if (
                target is not None
                and session.session_id is not None
                and _configured_method(controller, "tap_resolved") is not None
                and _configured_method(controller, "verify_focused_element") is not None
            ):
                target_details, expected_element = await _resolve_and_focus_target(_context, target)
                session.advance_screen_revision(operation=_context)
                target_package = (
                    expected_element.package if expected_element is not None else None
                )
                await _authorize_action(
                    _context,
                    controller,
                    action="clear_text",
                    target_package=target_package,
                    target_package_required=True,
                )
                detailed = _configured_method(controller, "clear_text_detailed")
                if detailed is not None:
                    result = detailed(
                        bounded_max_characters,
                        expected_element=expected_element,
                        focus_status="verified",
                    )
                else:
                    result = controller.clear_text(bounded_max_characters, None)
            else:
                await _authorize_action(
                    _context,
                    controller,
                    action="clear_text",
                    target_package_required=target is not None,
                )
                session.clear_snapshot()
                result = controller.clear_text(bounded_max_characters, target)
            if isawaitable(result):
                result = await result
            return result, target_details

        method, target_details = await session.run_operation(
            "clear_text",
            act,
            operation_id=operation_id,
            mutating=True,
        )
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _unexpected_failure("text clearing")
    data = {
        **_safe_input_data(method),
        "fallback_max_characters": bounded_max_characters,
    }
    if target_details is not None:
        data["target"] = target_details
        for key in ("selector", "matched_selector", "attempts"):
            if key in target_details:
                data[key] = target_details[key]
    return OperationResult(
        success=True,
        message=(
            "Text clear command completed and the field is empty."
            if data.get("effect_verified", False)
            else "Text clear command completed; observe the field to verify it is empty."
        ),
        data=data,
    ).model_dump(mode="json")


@mcp.tool(
    name="android_press_key",
    title="Press Android key",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_press_key(
    key: Annotated[
        str,
        Field(
            description=("One of: back, home, enter, delete, tab, menu, volume_up, volume_down.")
        ),
    ],
) -> dict[str, Any]:
    """Press back, home, enter, delete, tab, menu, or a volume key.

    Success means the key command was sent; observe again to verify any UI transition.
    """

    operation_id = session.new_operation_id()
    try:

        async def act(_context: Any) -> None:
            controller = session.require_controller()
            await _authorize_action(
                _context,
                controller,
                action="press_key",
                target_surface="launcher" if key.casefold() == "home" else None,
            )
            session.clear_snapshot()
            await controller.press_key(key)

        await session.run_operation(
            "press_key",
            act,
            operation_id=operation_id,
            mutating=True,
        )
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _unexpected_failure("key press")
    return OperationResult(success=True, message=f"Pressed Android key {key!r}.").model_dump(
        mode="json"
    )


@mcp.tool(
    name="android_launch_app",
    title="Launch Android app",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_launch_app(
    package: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            description="Exact installed Android package name, not a launcher display label.",
        ),
    ],
    activity: Annotated[
        str | None,
        Field(
            max_length=1_000,
            description="Optional exact launchable activity from android_list_apps.",
        ),
    ] = None,
    retries: Annotated[
        int,
        Field(ge=1, le=10, description="Maximum bounded launch command attempts."),
    ] = 3,
    timeout_seconds: Annotated[
        float,
        Field(gt=0, le=300, description="Foreground verification window per attempt."),
    ] = 15,
    stabilization_polls: Annotated[
        int,
        Field(ge=1, le=5, description="Consecutive foreground reads required for readiness."),
    ] = 2,
) -> dict[str, Any]:
    """Open an installed Android mobile App and wait until it reaches the foreground.

    Use this for requests such as 打开手机上的小红书 or open the shopping App. This tool requires an
    exact package; use android_list_apps with a known package fragment when package discovery is
    needed.
    """

    operation_id = session.new_operation_id()
    try:

        async def act(_context: Any) -> Any:
            controller = session.require_controller()
            await _authorize_action(
                _context,
                controller,
                action="launch_app",
                target_package=package,
            )
            session.clear_snapshot()
            if (
                activity is None
                and retries == 3
                and timeout_seconds == 15
                and stabilization_polls == 2
            ):
                return await controller.launch_app(package)
            return await controller.launch_app(
                package,
                activity=activity,
                retries=retries,
                timeout_seconds=timeout_seconds,
                stabilization_polls=stabilization_polls,
            )

        launch = await session.run_operation(
            "launch_app",
            act,
            operation_id=operation_id,
            mutating=True,
        )
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _unexpected_failure("app launch")
    if isinstance(launch, AppLaunchResult):
        data = launch.model_dump(mode="json")
    elif isinstance(launch, ForegroundApp):
        data = {
            "foreground_app": launch.model_dump(mode="json"),
            "requested_package": package,
            "command_status": "sent",
            "effect_status": "unverified",
            "verification_available": False,
            "stabilized": False,
            "requires_observation": True,
        }
    elif isinstance(launch, dict):
        data = cast(dict[str, Any], launch)
    else:
        data = {"requested_package": package, "effect_status": "unverified"}
    return OperationResult(
        success=True,
        message=f"Launched Android app {package!r}.",
        data=data,
    ).model_dump(mode="json")


@mcp.tool(
    name="android_terminate_app",
    title="Terminate Android app",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_terminate_app(
    package: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            description="Exact installed Android package name to force-stop.",
        ),
    ],
) -> dict[str, Any]:
    """Force-stop one exact package and report whether its effect was observable."""

    operation_id = session.new_operation_id()
    try:

        async def act(_context: Any) -> Any:
            controller = session.require_controller()
            await _authorize_action(
                _context,
                controller,
                action="terminate_app",
                target_package=package,
            )
            session.clear_snapshot()
            return await controller.terminate_app(package)

        termination = await session.run_operation(
            "terminate_app",
            act,
            operation_id=operation_id,
            mutating=True,
        )
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _unexpected_failure("app termination")
    if isinstance(termination, AppTerminationResult):
        data = termination.model_dump(mode="json")
    elif isinstance(termination, dict):
        data = cast(dict[str, Any], termination)
    else:
        data = {
            "requested_package": package,
            "targeted_package": package,
            "command_status": "sent",
            "effect_status": "unverified",
            "effect_verified": False,
            "verification_available": False,
            "requires_observation": True,
        }
    return OperationResult(
        success=True,
        message=f"Terminated Android app {package!r}.",
        data=data,
    ).model_dump(mode="json")


@mcp.tool(
    name="android_open_url",
    title="Open URL on Android",
    annotations=OPEN_WORLD_WRITE,
    structured_output=True,
)
async def android_open_url(
    url: Annotated[
        str,
        Field(
            min_length=8,
            max_length=4_096,
            description="Absolute http:// or https:// URL to open.",
        ),
    ],
) -> dict[str, Any]:
    """Send one absolute HTTP or HTTPS URL to the Android default browser.

    Success means the open command was sent; observe afterward for a chooser, permission dialog,
    browser loading state, or another foreground result.
    """

    operation_id = session.new_operation_id()
    try:

        async def act(_context: Any) -> None:
            controller = session.require_controller()
            await _authorize_action(
                _context,
                controller,
                action="open_url",
                target_surface="browser",
            )
            session.clear_snapshot()
            await controller.open_url(url)

        await session.run_operation(
            "open_url",
            act,
            operation_id=operation_id,
            mutating=True,
        )
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _unexpected_failure("URL open")
    return OperationResult(success=True, message="Opened the URL on Android.").model_dump(
        mode="json"
    )


@mcp.tool(
    name="android_start_recording",
    title="Start Android screen recording",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_start_recording(
    max_duration_seconds: Annotated[
        int,
        Field(
            ge=5,
            le=1_800,
            description="Maximum total recording duration before automatic stop.",
        ),
    ] = 300,
    bit_rate: Annotated[
        int | None,
        Field(
            ge=100_000,
            le=100_000_000,
            description="Optional device screenrecord bit rate in bits per second.",
        ),
    ] = None,
) -> RecordingStatusResult:
    """Start one bounded screen recording with automatic segment rollover.

    Call android_stop_recording to claim the completed artifact. A natural completion enters the
    ready state and remains retrievable with android_retrieve_recording. Do not start a second
    recording while one is active or while a ready artifact is unclaimed; disconnect aborts and
    cleans up unfinished recording state.
    """

    operation_id = session.new_operation_id()
    try:

        async def act(_context: Any) -> Any:
            session.clear_snapshot()
            return await session.start_recording(
                max_duration_seconds,
                bit_rate,
                operation=_context,
            )

        details = await session.run_operation(
            "start_recording",
            act,
            operation_id=operation_id,
            mutating=True,
        )
    except MobileUseError as error:
        return _recording_failure(error, operation_id=operation_id)
    except Exception:
        error = MobileUseError(
            ErrorCode.OPERATION_FAILED,
            "Android screen recording start failed.",
            "Check android_status and retry after confirming the device is ready.",
        )
        return _recording_failure(error, operation_id=operation_id)
    return _recording_result(
        details,
        operation_id=operation_id,
        default_state=RecordingState.RECORDING,
        message="Android screen recording started.",
    )


@mcp.tool(
    name="android_stop_recording",
    title="Stop Android screen recording",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_stop_recording(
    artifact_id: Annotated[
        str | None,
        Field(
            min_length=41,
            max_length=41,
            pattern=r"^artifact-[0-9a-f]{32}$",
            description=(
                "Optional ready artifact_id to claim after natural completion; omit to stop the "
                "active recording."
            ),
        ),
    ] = None,
) -> RecordingStatusResult:
    """Stop and claim the active recording, returning its retained artifact metadata.

    Calling this after natural completion retrieves the ready artifact. Multiple segment paths may
    be returned when ffmpeg is unavailable. Recordings can contain sensitive screen content.
    """

    operation_id = session.new_operation_id()
    try:

        async def act(_context: Any) -> Any:
            session.clear_snapshot()
            if artifact_id is None:
                return await session.stop_recording(operation=_context)
            return await session.stop_recording(artifact_id, operation=_context)

        details = await session.run_operation(
            "stop_recording",
            act,
            operation_id=operation_id,
            mutating=True,
        )
    except MobileUseError as error:
        return _recording_failure(error, operation_id=operation_id)
    except Exception:
        error = MobileUseError(
            ErrorCode.OPERATION_FAILED,
            "Android screen recording stop failed.",
            "Inspect android_status before retrying recording cleanup.",
        )
        return _recording_failure(error, operation_id=operation_id)
    return _recording_result(
        details,
        operation_id=operation_id,
        default_state=RecordingState.READY,
        message="Android screen recording stopped.",
    )


@mcp.tool(
    name="android_recording_status",
    title="Get Android recording status",
    annotations=READ_ONLY,
    structured_output=True,
)
async def android_recording_status() -> RecordingStatusResult:
    """Return recording state and metadata for the current session owner."""

    operation_id = session.new_operation_id()
    try:
        result = await session.run_operation(
            "recording_status",
            lambda _context: session.recording_status(operation_id),
            operation_id=operation_id,
            retry_safe=True,
            mutating=False,
        )
    except MobileUseError as error:
        return _recording_failure(error, operation_id=operation_id)
    except Exception:
        error = MobileUseError(
            ErrorCode.OPERATION_FAILED,
            "Android recording status could not be read.",
            "Retry android_recording_status after checking the MCP server.",
        )
        return _recording_failure(error, operation_id=operation_id)
    if isinstance(result, RecordingStatusResult):
        return result.model_copy(update={"operation_id": operation_id}, deep=True)
    return _recording_result(
        result,
        operation_id=operation_id,
        default_state=RecordingState.IDLE,
        message="Android recording status read.",
    )


# Compatibility spelling for clients that prefer a verb in read tool names.
android_get_recording_status = android_recording_status


@mcp.tool(
    name="android_retrieve_recording",
    title="Retrieve Android recording artifact",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_retrieve_recording(
    artifact_id: Annotated[
        str,
        Field(
            min_length=41,
            max_length=41,
            pattern=r"^artifact-[0-9a-f]{32}$",
            description="Opaque artifact_id returned by Android recording tools.",
        ),
    ],
) -> RecordingStatusResult:
    """Claim one retained recording by server-generated ID.

    The client cannot provide a local path. The manager validates ownership and containment before
    returning metadata and leaves the artifact subject to the configured TTL/count/byte policy.
    """

    operation_id = session.new_operation_id()
    try:
        details = await session.run_operation(
            "retrieve_recording",
            lambda _context: session.retrieve_recording(artifact_id),
            operation_id=operation_id,
            retry_safe=True,
            mutating=True,
        )
    except MobileUseError as error:
        return _recording_failure(error, operation_id=operation_id)
    except Exception:
        error = MobileUseError(
            ErrorCode.OPERATION_FAILED,
            "Android recording artifact retrieval failed.",
            "Use a retained artifact_id from android_recording_status and retry.",
        )
        return _recording_failure(error, operation_id=operation_id)
    if isinstance(details, RecordingStatusResult):
        return details.model_copy(update={"operation_id": operation_id}, deep=True)
    return _recording_result(
        details,
        operation_id=operation_id,
        default_state=RecordingState.READY,
        message="Android recording artifact retrieved.",
    )


# Compatibility spelling for callers that use ``get`` rather than
# ``retrieve`` for a retained artifact.
android_get_recording = android_retrieve_recording


@mcp.tool(
    name="android_wait_for_text",
    title="Wait for Android text",
    annotations=READ_ONLY,
    structured_output=True,
)
async def android_wait_for_text(
    text: Annotated[
        str,
        Field(
            min_length=1,
            max_length=2_000,
            description=(
                "Case-insensitive substring to find in a complete Android hierarchy's text "
                "or content description. The value is never echoed in results."
            ),
        ),
    ],
    text_index: Annotated[
        int,
        Field(
            ge=0,
            le=10_000,
            description="Zero-based occurrence among matching text/content descriptions.",
        ),
    ] = 0,
    timeout_seconds: Annotated[
        float,
        Field(
            gt=0,
            le=3_600,
            description="Total monotonic wait budget, including the operation queue (default 10s).",
        ),
    ] = DEFAULT_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: Annotated[
        float,
        Field(
            gt=0,
            le=MAX_WAIT_POLL_INTERVAL_SECONDS,
            description="Bounded polling interval in seconds (default 0.25s).",
        ),
    ] = DEFAULT_WAIT_POLL_INTERVAL_SECONDS,
) -> WaitResult:
    """Wait until text or a content description appears on Android.

    Each poll reads the complete hierarchy, not the compact response page.
    On success the result points at the immutable final snapshot that matched.
    A timeout is returned as an MCP error with bounded poll and last-observation
    diagnostics. Use android_wait for a fixed delay when no condition is needed.
    """

    return await _call_condition_wait(
        condition="text",
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        matcher=lambda snapshot: _wait_text_match(snapshot, text, text_index),
    )


@mcp.tool(
    name="android_wait_for_element",
    title="Wait for Android element",
    annotations=READ_ONLY,
    structured_output=True,
)
async def android_wait_for_element(
    target: Annotated[
        Target,
        Field(
            description=(
                "Element presence selector. Prefer resource_id or text/content-description; "
                "the selector is evaluated against the complete current hierarchy."
            )
        ),
    ],
    timeout_seconds: Annotated[
        float,
        Field(
            gt=0,
            le=3_600,
            description="Total monotonic wait budget, including the operation queue (default 10s).",
        ),
    ] = DEFAULT_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: Annotated[
        float,
        Field(
            gt=0,
            le=MAX_WAIT_POLL_INTERVAL_SECONDS,
            description="Bounded polling interval in seconds (default 0.25s).",
        ),
    ] = DEFAULT_WAIT_POLL_INTERVAL_SECONDS,
) -> WaitResult:
    """Wait until a target element appears in the complete Android hierarchy.

    This is a presence wait, so a matching disabled or bounds-less node still
    satisfies the condition. It does not tap, focus, or otherwise mutate the
    device.
    """

    return await _call_condition_wait(
        condition="element",
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        matcher=lambda snapshot: _wait_element_match(snapshot, target),
    )


@mcp.tool(
    name="android_wait_for_ui_change",
    title="Wait for Android UI change",
    annotations=READ_ONLY,
    structured_output=True,
)
async def android_wait_for_ui_change(
    baseline_snapshot_id: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=128,
            description=(
                "Explicit snapshot_id from android_snapshot to compare against; use this for "
                "content-level change detection."
            ),
        ),
    ] = None,
    baseline_screen_revision: Annotated[
        int | None,
        Field(
            ge=0,
            description=(
                "Explicit session screen revision baseline. The wait succeeds when a later "
                "observation has a different revision."
            ),
        ),
    ] = None,
    baseline_revision: Annotated[
        int | None,
        Field(
            ge=0,
            description="Compatibility alias for baseline_screen_revision.",
        ),
    ] = None,
    timeout_seconds: Annotated[
        float,
        Field(
            gt=0,
            le=3_600,
            description="Total monotonic wait budget, including the operation queue (default 10s).",
        ),
    ] = DEFAULT_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: Annotated[
        float,
        Field(
            gt=0,
            le=MAX_WAIT_POLL_INTERVAL_SECONDS,
            description="Bounded polling interval in seconds (default 0.25s).",
        ),
    ] = DEFAULT_WAIT_POLL_INTERVAL_SECONDS,
) -> WaitResult:
    """Wait until Android UI content differs from an explicit baseline.

    Supply a baseline snapshot ID or a screen revision (or both). A successful
    result references the immutable final snapshot; a timeout is an MCP error
    with bounded poll count, elapsed time, and last safe observation metadata.
    """

    if baseline_revision is not None:
        if (
            baseline_screen_revision is not None
            and baseline_revision != baseline_screen_revision
        ):
            raise ValueError("baseline_revision and baseline_screen_revision must agree")
        baseline_screen_revision = baseline_revision
    if baseline_snapshot_id is None and baseline_screen_revision is None:
        raise ValueError("baseline_snapshot_id or baseline_screen_revision is required")

    return await _call_condition_wait(
        condition="ui_change",
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        matcher=None,
        baseline_snapshot_id=baseline_snapshot_id,
        baseline_screen_revision=baseline_screen_revision,
    )


@mcp.tool(
    name="android_wait",
    title="Wait for Android UI",
    annotations=READ_ONLY,
    structured_output=True,
)
async def android_wait(
    milliseconds: Annotated[
        int,
        Field(
            ge=0,
            le=60_000,
            description="Fixed sleep duration; this does not wait for any UI condition.",
        ),
    ] = 1_000,
) -> dict[str, Any]:
    """Sleep for a fixed delay and invalidate the cached snapshot.

    This is not a condition wait and does not observe whether an element appeared. Call
    android_snapshot afterward and do not reuse an earlier snapshot_id.
    """

    operation_id = session.new_operation_id()
    try:

        async def wait_fixed(context: Any) -> None:
            session.clear_snapshot()
            await context.sleep(milliseconds / 1_000)

        await session.run_operation(
            "wait",
            wait_fixed,
            operation_id=operation_id,
            retry_safe=True,
            mutating=True,
        )
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    return OperationResult(
        success=True,
        message=f"Waited {milliseconds} milliseconds.",
        data={"milliseconds": milliseconds},
    ).model_dump(mode="json")


def main() -> None:
    """Validate runtime settings and run the MCP server over stdio."""

    try:
        configure_runtime(load_runtime_config())
    except ConfigurationError as error:
        # Do not let pydantic include invalid values (which may be paths,
        # credentials, or endpoint details) in the startup diagnostic.  A
        # non-zero exit also prevents a silent fallback to local defaults.
        print(format_startup_error(error), file=sys.stderr)
        raise SystemExit(2) from None

    # FastMCP owns the asyncio lifespan context, so make SIGTERM unwind that
    # context instead of letting the interpreter terminate immediately.  The
    # lifespan's ``finally`` then awaits the session's idempotent close path,
    # including recording-child termination and final reaping.  Restore the
    # embedding process's handler after the synchronous runner returns.
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def request_shutdown(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, request_shutdown)
    try:
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        # SIGINT and SIGTERM both use the normal FastMCP lifespan unwind.  Do
        # not emit a traceback after cleanup has completed.
        return
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    main()
