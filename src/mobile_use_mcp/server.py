"""stdio MCP server entry point and Android observation tools."""

import asyncio
import json
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from mobile_use_mcp.errors import MobileUseError
from mobile_use_mcp.images import encode_screenshot
from mobile_use_mcp.models import OperationResult, Target, UIElement
from mobile_use_mcp.session import SessionManager

mcp = FastMCP("mobile_use_mcp")
session = SessionManager()

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


def _failure(error: MobileUseError) -> OperationResult:
    return OperationResult(
        success=False,
        error_code=error.code,
        message=error.message,
        suggestion=error.suggestion,
        data=error.data,
    )


@mcp.tool(
    name="android_list_devices",
    title="List Android devices",
    annotations=READ_ONLY,
    structured_output=True,
)
async def android_list_devices(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """List Android devices known to ADB, including offline and unauthorized devices."""

    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    try:
        devices = await asyncio.to_thread(session.registry.list_devices)
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    page = devices[offset : offset + limit]
    return {
        "success": True,
        "total": len(devices),
        "count": len(page),
        "offset": offset,
        "has_more": offset + len(page) < len(devices),
        "next_offset": offset + len(page) if offset + len(page) < len(devices) else None,
        "devices": [device.model_dump(mode="json") for device in page],
    }


@mcp.tool(
    name="android_connect",
    title="Connect Android device",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_connect(serial: str | None = None) -> dict[str, Any]:
    """Connect the MCP session to one authorized Android device."""

    try:
        device = await asyncio.to_thread(session.connect, serial)
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return OperationResult(
            success=False,
            error_code="OPERATION_FAILED",
            message="Failed to initialize the Android automation connection.",
            suggestion="Check `adb devices -l`, unlock the device, and try again.",
        ).model_dump(mode="json")
    return OperationResult(
        success=True,
        message=f"Connected to Android device {device.serial}.",
        data={"device": device.model_dump(mode="json")},
    ).model_dump(mode="json")


@mcp.tool(
    name="android_status",
    title="Get Android session status",
    annotations=READ_ONLY,
    structured_output=True,
)
async def android_status() -> dict[str, Any]:
    """Return the currently selected Android device without changing session state."""

    if session.device is None:
        return {
            "success": True,
            "connected": False,
            "device": None,
            "message": "No Android device is connected.",
        }
    return {
        "success": True,
        "connected": True,
        "device": session.device.model_dump(mode="json"),
        "message": f"Connected to Android device {session.device.serial}.",
    }


@mcp.tool(
    name="android_disconnect",
    title="Disconnect Android device",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_disconnect() -> dict[str, Any]:
    """Disconnect the current Android session and release uiautomator2 state."""

    await asyncio.to_thread(session.disconnect)
    return OperationResult(success=True, message="Android device disconnected.").model_dump(
        mode="json"
    )


@mcp.tool(
    name="android_snapshot",
    title="Observe Android screen",
    annotations=READ_ONLY,
)
async def android_snapshot(
    detail_level: Literal["compact", "full"] = "compact",
    interactive_only: bool = False,
    max_elements: int = 200,
    max_text_length: int = 500,
    image_format: Literal["png", "jpeg"] = "png",
    image_quality: int = 80,
    max_width: int | None = None,
) -> CallToolResult:
    """Return a screenshot and either bounded or full normalized UI elements."""

    if not 1 <= max_elements <= 500:
        raise ValueError("max_elements must be between 1 and 500")
    if not 1 <= max_text_length <= 2_000:
        raise ValueError("max_text_length must be between 1 and 2000")
    if not 1 <= image_quality <= 100:
        raise ValueError("image_quality must be between 1 and 100")
    if max_width is not None and not 240 <= max_width <= 4_096:
        raise ValueError("max_width must be between 240 and 4096")
    try:
        controller = session.require_controller()
        snapshot = await controller.snapshot(
            interactive_only=interactive_only,
            max_elements=None,
            max_text_length=max_text_length,
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

    snapshot_id = session.store_snapshot(snapshot)
    total_elements = len(snapshot.elements)
    returned_elements = (
        snapshot.elements if detail_level == "full" else snapshot.elements[:max_elements]
    )
    truncated = len(returned_elements) < total_elements
    image_data, image_width, image_height = encode_screenshot(
        snapshot.screenshot_png,
        image_format=image_format,
        image_quality=image_quality,
        max_width=max_width,
    )
    metadata = {
        "success": True,
        "snapshot_id": snapshot_id,
        "detail_level": detail_level,
        "serial": snapshot.serial,
        "width": snapshot.width,
        "height": snapshot.height,
        "image_format": image_format,
        "image_width": image_width,
        "image_height": image_height,
        "foreground_app": snapshot.foreground_app.model_dump(mode="json"),
        "total_elements": total_elements,
        "returned_elements": len(returned_elements),
        "element_count": len(returned_elements),
        "truncated": truncated,
        "next_offset": len(returned_elements) if truncated else None,
        "full_fallback": (
            {"tool": "android_snapshot", "arguments": {"detail_level": "full"}}
            if truncated
            else None
        ),
        "elements": [element.model_dump(mode="json") for element in returned_elements],
    }
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
    image_format: Literal["png", "jpeg"] = "png",
    image_quality: int = 80,
    max_width: int | None = None,
) -> CallToolResult:
    """Return a configurable Android screenshot and basic screen metadata."""

    if not 1 <= image_quality <= 100:
        raise ValueError("image_quality must be between 1 and 100")
    if max_width is not None and not 240 <= max_width <= 4_096:
        raise ValueError("max_width must be between 240 and 4096")

    try:
        snapshot = await session.require_controller().snapshot(
            interactive_only=True,
            max_elements=1,
            max_text_length=1,
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
        max_width=max_width,
    )
    metadata = {
        "success": True,
        "serial": snapshot.serial,
        "width": snapshot.width,
        "height": snapshot.height,
        "image_format": image_format,
        "image_width": image_width,
        "image_height": image_height,
        "foreground_app": snapshot.foreground_app.model_dump(mode="json"),
    }
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
    interactive_only: bool = False,
) -> dict[str, Any]:
    """Page and query elements from one cached snapshot, or capture a new snapshot."""

    try:
        if snapshot_id is None:
            snapshot = await session.require_controller().snapshot(
                interactive_only=False,
                max_elements=None,
                max_text_length=2_000,
            )
            resolved_snapshot_id = session.store_snapshot(snapshot)
        else:
            snapshot = session.get_snapshot(snapshot_id)
            resolved_snapshot_id = snapshot_id
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
        "width": snapshot.width,
        "height": snapshot.height,
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

    try:
        foreground = await session.require_controller().get_foreground_app()
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _observation_failure("read the foreground Android app")
    return {
        "success": True,
        "foreground_app": foreground.model_dump(mode="json"),
    }


@mcp.tool(
    name="android_list_apps",
    title="List installed Android apps",
    annotations=READ_ONLY,
    structured_output=True,
)
async def android_list_apps(query: str | None = None, limit: int = 100) -> dict[str, Any]:
    """List third-party package names, optionally filtered by a case-insensitive query."""

    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    try:
        apps = await session.require_controller().list_apps(query=query, limit=limit)
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _observation_failure("list installed Android apps")
    return {"success": True, "count": len(apps), "apps": apps}


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


@mcp.tool(
    name="android_tap",
    title="Tap Android UI target",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_tap(target: Target) -> dict[str, Any]:
    """Tap a target using bounds, resource ID, then text as selector fallbacks."""

    try:
        async with session.write_lock:
            session.clear_snapshot()
            details = await session.require_controller().tap(target)
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
    duration_ms: Annotated[int, Field(ge=100, le=10_000)] = 1_000,
) -> dict[str, Any]:
    """Long press a target for a bounded duration in milliseconds."""

    try:
        async with session.write_lock:
            session.clear_snapshot()
            details = await session.require_controller().long_press(target, duration_ms)
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
    start_x: Annotated[int, Field(ge=0)],
    start_y: Annotated[int, Field(ge=0)],
    end_x: Annotated[int, Field(ge=0)],
    end_y: Annotated[int, Field(ge=0)],
    duration_ms: Annotated[int, Field(ge=1, le=10_000)] = 400,
) -> dict[str, Any]:
    """Swipe between two pixel coordinates validated against the current screen."""

    try:
        async with session.write_lock:
            session.clear_snapshot()
            await session.require_controller().swipe(
                start_x,
                start_y,
                end_x,
                end_y,
                duration_ms,
            )
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _unexpected_failure("swipe")
    return OperationResult(
        success=True,
        message=f"Swiped from ({start_x}, {start_y}) to ({end_x}, {end_y}).",
        data={
            "start": {"x": start_x, "y": start_y},
            "end": {"x": end_x, "y": end_y},
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
    text: Annotated[str, Field(min_length=1, max_length=10_000)],
    target: Target | None = None,
) -> dict[str, Any]:
    """Optionally focus a target, then type text into the focused Android field."""

    try:
        async with session.write_lock:
            session.clear_snapshot()
            method = await session.require_controller().type_text(text, target)
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _unexpected_failure("text input")
    return OperationResult(
        success=True,
        message="Text was entered into the focused field.",
        data={"method": method, "character_count": len(text)},
    ).model_dump(mode="json")


@mcp.tool(
    name="android_clear_text",
    title="Clear focused Android text",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_clear_text(
    max_characters: Annotated[int, Field(ge=1, le=2_000)] = 100,
    target: Target | None = None,
) -> dict[str, Any]:
    """Optionally focus a target, then delete bounded characters from the field."""

    try:
        async with session.write_lock:
            session.clear_snapshot()
            method = await session.require_controller().clear_text(max_characters, target)
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _unexpected_failure("text clearing")
    return OperationResult(
        success=True,
        message="Cleared the focused Android text field.",
        data={"method": method, "fallback_max_characters": max_characters},
    ).model_dump(mode="json")


@mcp.tool(
    name="android_press_key",
    title="Press Android key",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_press_key(key: str) -> dict[str, Any]:
    """Press one allowed Android key such as back, home, enter, delete, or tab."""

    try:
        async with session.write_lock:
            session.clear_snapshot()
            await session.require_controller().press_key(key)
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
    package: Annotated[str, Field(min_length=1, max_length=512)],
) -> dict[str, Any]:
    """Launch an installed package and wait until it reaches the foreground."""

    try:
        async with session.write_lock:
            session.clear_snapshot()
            launch = await session.require_controller().launch_app(package)
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _unexpected_failure("app launch")
    return OperationResult(
        success=True,
        message=f"Launched Android app {package!r}.",
        data=launch.model_dump(mode="json"),
    ).model_dump(mode="json")


@mcp.tool(
    name="android_terminate_app",
    title="Terminate Android app",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_terminate_app(
    package: Annotated[str, Field(min_length=1, max_length=512)],
) -> dict[str, Any]:
    """Force-stop one installed Android package."""

    try:
        async with session.write_lock:
            session.clear_snapshot()
            await session.require_controller().terminate_app(package)
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _unexpected_failure("app termination")
    return OperationResult(success=True, message=f"Terminated Android app {package!r}.").model_dump(
        mode="json"
    )


@mcp.tool(
    name="android_open_url",
    title="Open URL on Android",
    annotations=OPEN_WORLD_WRITE,
    structured_output=True,
)
async def android_open_url(
    url: Annotated[str, Field(min_length=8, max_length=4_096)],
) -> dict[str, Any]:
    """Open one absolute HTTP or HTTPS URL in the Android default browser."""

    try:
        async with session.write_lock:
            session.clear_snapshot()
            await session.require_controller().open_url(url)
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
    max_duration_seconds: Annotated[int, Field(ge=5, le=1_800)] = 300,
    bit_rate: Annotated[int | None, Field(ge=100_000, le=100_000_000)] = None,
) -> dict[str, Any]:
    """Start a bounded Android screen recording that rolls over into safe segments."""

    try:
        async with session.write_lock:
            session.clear_snapshot()
            details = await session.require_controller().start_recording(
                max_duration_seconds,
                bit_rate,
            )
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _unexpected_failure("screen recording start")
    return OperationResult(
        success=True,
        message="Android screen recording started.",
        data=details,
    ).model_dump(mode="json")


@mcp.tool(
    name="android_stop_recording",
    title="Stop Android screen recording",
    annotations=SESSION_WRITE,
    structured_output=True,
)
async def android_stop_recording() -> dict[str, Any]:
    """Stop the active recording, pull its segments, and merge them when ffmpeg is available."""

    try:
        async with session.write_lock:
            session.clear_snapshot()
            details = await session.require_controller().stop_recording()
    except MobileUseError as error:
        return _failure(error).model_dump(mode="json")
    except Exception:
        return _unexpected_failure("screen recording stop")
    return OperationResult(
        success=True,
        message="Android screen recording stopped.",
        data=details,
    ).model_dump(mode="json")


@mcp.tool(
    name="android_wait",
    title="Wait for Android UI",
    annotations=READ_ONLY,
    structured_output=True,
)
async def android_wait(
    milliseconds: Annotated[int, Field(ge=0, le=60_000)] = 1_000,
) -> dict[str, Any]:
    """Wait for a bounded delay before observing the Android UI again."""

    session.clear_snapshot()
    await asyncio.sleep(milliseconds / 1_000)
    return OperationResult(
        success=True,
        message=f"Waited {milliseconds} milliseconds.",
        data={"milliseconds": milliseconds},
    ).model_dump(mode="json")


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
