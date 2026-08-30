"""Run the real public-stdio acceptance flow against the Android fixture.

The command is intentionally a host-side harness, not another MCP client
implementation.  It starts an independently installed server with
``StdioServerParameters`` and talks to that process only through the official
``ClientSession`` API.  Device setup (SDK, ADB authorization, and fixture APK
installation) belongs to the caller or CI job; an explicit serial is always
required here.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import struct
import sys
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, ImageContent

FIXTURE_PACKAGE = "com.neoagentman.mobileusefixture"
FIXTURE_RESOURCE_PREFIX = f"{FIXTURE_PACKAGE}:id"
FIXTURE_IDS = {
    "title": f"{FIXTURE_RESOURCE_PREFIX}/fixture_title",
    "change_button": f"{FIXTURE_RESOURCE_PREFIX}/fixture_change_button",
    "change_value": f"{FIXTURE_RESOURCE_PREFIX}/fixture_change_value",
    "delayed_trigger": f"{FIXTURE_RESOURCE_PREFIX}/fixture_delayed_trigger",
    "delayed_element": f"{FIXTURE_RESOURCE_PREFIX}/fixture_delayed_element",
    "text_input": f"{FIXTURE_RESOURCE_PREFIX}/fixture_text_input",
    "reset_button": f"{FIXTURE_RESOURCE_PREFIX}/fixture_reset_button",
    "scroll_container": f"{FIXTURE_RESOURCE_PREFIX}/fixture_scroll_container",
    "scroll_state": f"{FIXTURE_RESOURCE_PREFIX}/fixture_scroll_state",
}

REQUIRED_PUBLIC_TOOLS = {
    "android_connect",
    "android_disconnect",
    "android_get_ui_elements",
    "android_list_apps",
    "android_launch_app",
    "android_press_key",
    "android_snapshot",
    "android_swipe",
    "android_tap",
    "android_type_text",
    "android_clear_text",
    "android_wait_for_element",
    "android_wait_for_text",
    "android_wait_for_ui_change",
    "android_recording_status",
    "android_stop_recording",
}


class AcceptanceError(RuntimeError):
    """Safe, stage-labelled failure without dumping tool payloads."""


def _require(condition: bool, stage: str, message: str) -> None:
    if not condition:
        raise AcceptanceError(f"{stage}: {message}")


def _payload(result: CallToolResult, stage: str) -> dict[str, Any]:
    value = result.structuredContent
    _require(isinstance(value, dict), stage, "the server returned no structured payload")
    return value


def _success(result: CallToolResult, stage: str) -> dict[str, Any]:
    payload = _payload(result, stage)
    _require(not result.isError, stage, "the public tool returned an MCP business error")
    _require(payload.get("success") is True, stage, "the public tool did not report success")
    return payload


def _failure(result: CallToolResult, stage: str, error_code: str) -> dict[str, Any]:
    payload = _payload(result, stage)
    _require(result.isError is True, stage, "the expected business failure was not an MCP error")
    _require(payload.get("success") is False, stage, "failure payload did not report success=false")
    _require(payload.get("error_code") == error_code, stage, "unexpected typed error code")
    for field in ("category", "retryable", "message", "suggestion", "details", "operation_id"):
        _require(field in payload, stage, f"failure payload is missing {field}")
    return payload


def _resource_target(resource_id: str) -> dict[str, Any]:
    return {"resource_id": resource_id}


def _find_element(payload: Mapping[str, Any], resource_id: str, stage: str) -> dict[str, Any]:
    elements = payload.get("elements")
    _require(isinstance(elements, list), stage, "the UI page has no element list")
    for item in elements:
        if isinstance(item, dict) and item.get("resource_id") == resource_id:
            return item
    raise AcceptanceError(f"{stage}: fixture resource ID was not present in the UI page")


def _find_text(payload: Mapping[str, Any], resource_id: str, stage: str) -> str | None:
    element = _find_element(payload, resource_id, stage)
    value = element.get("text")
    _require(value is None or isinstance(value, str), stage, "element text has an invalid type")
    return value


def _assert_provenance(
    payload: Mapping[str, Any], expected_session: str, expected_generation: int, stage: str
) -> int:
    session_id = payload.get("session_id")
    generation = payload.get("generation")
    revision = payload.get("screen_revision")
    _require(session_id == expected_session, stage, "session provenance changed unexpectedly")
    _require(generation == expected_generation, stage, "session generation changed unexpectedly")
    _require(isinstance(revision, int) and revision >= 0, stage, "screen revision is invalid")
    return revision


def _verify_png_image(result: CallToolResult, payload: Mapping[str, Any], stage: str) -> None:
    image_blocks = [item for item in result.content if isinstance(item, ImageContent)]
    _require(len(image_blocks) == 1, stage, "snapshot did not return one native image block")
    block = image_blocks[0]
    _require(getattr(block, "mimeType", None) == "image/png", stage, "snapshot image is not PNG")
    encoded = getattr(block, "data", None)
    _require(isinstance(encoded, str) and encoded, stage, "native image content is empty")
    try:
        image_data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise AcceptanceError(f"{stage}: native image content is not valid base64") from None
    _require(image_data.startswith(b"\x89PNG\r\n\x1a\n"), stage, "native image is not a PNG")
    _require(len(image_data) >= 24, stage, "native image is truncated")
    width, height = struct.unpack(">II", image_data[16:24])
    _require(width > 0 and height > 0, stage, "native image has invalid dimensions")
    _require(payload.get("image_format") == "png", stage, "structured image format is not PNG")
    _require(payload.get("image_lossless") is True, stage, "PNG was not marked lossless")
    _require(payload.get("image_width") == width, stage, "image width metadata disagrees")
    _require(payload.get("image_height") == height, stage, "image height metadata disagrees")


async def _call(
    client: ClientSession,
    name: str,
    arguments: Mapping[str, Any] | None,
    *,
    timeout_seconds: float,
    stage: str,
) -> CallToolResult:
    """Call one public tool with both client and harness deadlines."""

    try:
        return await asyncio.wait_for(
            client.call_tool(
                name,
                arguments=dict(arguments or {}),
                read_timeout_seconds=timedelta(seconds=timeout_seconds),
            ),
            timeout=timeout_seconds + 1,
        )
    except TimeoutError:
        raise AcceptanceError(f"{stage}: public tool deadline exceeded") from None


async def _best_effort_call(
    client: ClientSession,
    name: str,
    arguments: Mapping[str, Any] | None,
    *,
    timeout_seconds: float,
    stage: str,
) -> CallToolResult | None:
    """Attempt cleanup without exposing a failed device payload."""

    try:
        return await _call(
            client,
            name,
            arguments,
            timeout_seconds=timeout_seconds,
            stage=stage,
        )
    except Exception:
        return None


async def _reset_and_return_home(
    client: ClientSession,
    *,
    package: str,
    timeout_seconds: float,
) -> bool:
    """Reset the fixture and return Home using only public tools."""

    launch = await _best_effort_call(
        client,
        "android_launch_app",
        {"package": package},
        timeout_seconds=timeout_seconds,
        stage="cleanup.launch_fixture",
    )
    ready = await _best_effort_call(
        client,
        "android_wait_for_element",
        {"target": _resource_target(FIXTURE_IDS["reset_button"]), "timeout_seconds": 8},
        timeout_seconds=timeout_seconds,
        stage="cleanup.wait_reset_button",
    )
    reset = await _best_effort_call(
        client,
        "android_tap",
        {"target": _resource_target(FIXTURE_IDS["reset_button"])},
        timeout_seconds=timeout_seconds,
        stage="cleanup.reset_fixture",
    )
    reset_wait = await _best_effort_call(
        client,
        "android_wait_for_text",
        {"text": "Fixture reset", "timeout_seconds": 8, "poll_interval_seconds": 0.25},
        timeout_seconds=timeout_seconds,
        stage="cleanup.wait_reset",
    )
    home = await _best_effort_call(
        client,
        "android_press_key",
        {"key": "home"},
        timeout_seconds=timeout_seconds,
        stage="cleanup.home",
    )
    return all(
        result is not None
        and result.isError is not True
        and isinstance(result.structuredContent, dict)
        and result.structuredContent.get("success") is True
        for result in (launch, ready, reset, reset_wait, home)
    )


async def _cleanup(
    client: ClientSession,
    *,
    package: str,
    connect_attempted: bool,
    timeout_seconds: float,
) -> tuple[CallToolResult | None, bool]:
    """Run unconditional, public-tool-only cleanup and return disconnect evidence."""

    if not connect_attempted:
        return None, False

    recording = await _best_effort_call(
        client,
        "android_recording_status",
        None,
        timeout_seconds=timeout_seconds,
        stage="cleanup.recording_status",
    )
    if (
        recording is not None
        and isinstance(recording.structuredContent, dict)
        and recording.structuredContent.get("state") == "recording"
    ):
        await _best_effort_call(
            client,
            "android_stop_recording",
            None,
            timeout_seconds=timeout_seconds,
            stage="cleanup.stop_recording",
        )

    reset_ok = await _reset_and_return_home(
        client,
        package=package,
        timeout_seconds=timeout_seconds,
    )
    disconnect = await _best_effort_call(
        client,
        "android_disconnect",
        None,
        timeout_seconds=timeout_seconds,
        stage="cleanup.disconnect",
    )
    return disconnect, reset_ok


async def _cleanup_shielded(
    client: ClientSession,
    *,
    package: str,
    connect_attempted: bool,
    timeout_seconds: float,
) -> tuple[CallToolResult | None, bool]:
    """Keep disconnect cleanup running when the acceptance task is cancelled."""

    cleanup_task = asyncio.create_task(
        _cleanup(
            client,
            package=package,
            connect_attempted=connect_attempted,
            timeout_seconds=timeout_seconds,
        ),
        name="android-fixture-acceptance-cleanup",
    )
    try:
        return await asyncio.shield(cleanup_task)
    except asyncio.CancelledError:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(cleanup_task)
        raise


async def _workflow(
    client: ClientSession,
    *,
    serial: str,
    package: str,
    stage_timeout_seconds: float,
) -> None:
    """Exercise the complete fixture contract through public MCP calls."""

    listed = await client.list_tools()
    available = {tool.name for tool in listed.tools}
    _require(
        available >= REQUIRED_PUBLIC_TOOLS,
        "tools/list",
        "the installed server is missing required public tools",
    )

    connected = _success(
        await _call(
            client,
            "android_connect",
            {
                "serial": serial,
                "allowed_packages": [package],
                "allowed_system_surfaces": ["launcher"],
            },
            timeout_seconds=stage_timeout_seconds,
            stage="connect",
        ),
        "connect",
    )
    _require(connected.get("connected") is True, "connect", "device was not marked connected")
    session_id = connected.get("session_id")
    generation = connected.get("generation")
    _require(isinstance(session_id, str) and session_id, "connect", "session ID is missing")
    _require(isinstance(generation, int) and generation >= 1, "connect", "generation is missing")
    policy = connected.get("policy")
    _require(isinstance(policy, dict), "connect", "package policy summary is missing")
    _require(policy.get("mode") == "allowlist", "connect", "fixture connection is unrestricted")
    _require(
        package in policy.get("allowed_packages", []),
        "connect",
        "fixture package is not scoped",
    )

    inventory = _success(
        await _call(
            client,
            "android_list_apps",
            {"query": package, "limit": 20},
            timeout_seconds=stage_timeout_seconds,
            stage="app_inventory",
        ),
        "app_inventory",
    )
    apps = inventory.get("apps")
    _require(
        isinstance(apps, list)
        and any(isinstance(app, dict) and app.get("package") == package for app in apps),
        "app_inventory",
        "fixture package was not found in installed-app inventory",
    )

    launched = _success(
        await _call(
            client,
            "android_launch_app",
            {"package": package},
            timeout_seconds=max(stage_timeout_seconds, 45),
            stage="launch_fixture",
        ),
        "launch_fixture",
    )
    foreground = launched.get("data", {}).get("foreground_app", {})
    _require(
        isinstance(foreground, dict) and foreground.get("package") == package,
        "launch_fixture",
        "fixture did not become the foreground app",
    )

    ready = _success(
        await _call(
            client,
            "android_wait_for_element",
            {
                "target": _resource_target(FIXTURE_IDS["title"]),
                "timeout_seconds": 15,
                "poll_interval_seconds": 0.25,
            },
            timeout_seconds=max(stage_timeout_seconds, 30),
            stage="wait_ready",
        ),
        "wait_ready",
    )
    _assert_provenance(ready, session_id, generation, "wait_ready")

    baseline_result = await _call(
        client,
        "android_snapshot",
        {
            "detail_level": "compact",
            "max_elements": 4,
            "max_text_length": 500,
            "image_format": "png",
        },
        timeout_seconds=stage_timeout_seconds,
        stage="snapshot",
    )
    baseline = _success(baseline_result, "snapshot")
    baseline_revision = _assert_provenance(baseline, session_id, generation, "snapshot")
    baseline_id = baseline.get("snapshot_id")
    _require(isinstance(baseline_id, str) and baseline_id, "snapshot", "snapshot ID is missing")
    _require(baseline.get("truncated") is True, "snapshot", "compact snapshot did not page")
    _verify_png_image(baseline_result, baseline, "snapshot")

    first_page = _success(
        await _call(
            client,
            "android_get_ui_elements",
            {"snapshot_id": baseline_id, "offset": 0, "limit": 2},
            timeout_seconds=stage_timeout_seconds,
            stage="snapshot_paging",
        ),
        "snapshot_paging",
    )
    _require(
        first_page.get("snapshot_id") == baseline_id,
        "snapshot_paging",
        "page changed snapshot",
    )
    _require(
        isinstance(first_page.get("elements"), list) and first_page.get("count", 0) <= 2,
        "snapshot_paging",
        "snapshot page exceeded its requested limit",
    )

    scroll_page = _success(
        await _call(
            client,
            "android_get_ui_elements",
            {
                "snapshot_id": baseline_id,
                "query": "fixture_scroll_container",
                "limit": 5,
            },
            timeout_seconds=stage_timeout_seconds,
            stage="scroll_target",
        ),
        "scroll_target",
    )
    scroll_element = _find_element(scroll_page, FIXTURE_IDS["scroll_container"], "scroll_target")
    _require(
        scroll_element.get("scrollable") is True,
        "scroll_target",
        "fixture region is not scrollable",
    )
    _require(
        scroll_element.get("element_ref"),
        "scroll_target",
        "scroll target has no element ref",
    )

    swipe = _success(
        await _call(
            client,
            "android_swipe",
            {
                "percentage": {
                    "start_x": 0.5,
                    "start_y": 0.84,
                    "end_x": 0.5,
                    "end_y": 0.20,
                },
                "duration_ms": 350,
            },
            timeout_seconds=stage_timeout_seconds,
            stage="percentage_swipe",
        ),
        "percentage_swipe",
    )
    _require(swipe.get("mode") == "percentage", "percentage_swipe", "swipe mode was not percentage")
    swipe_data = swipe.get("data")
    _require(isinstance(swipe_data, dict), "percentage_swipe", "swipe coordinate evidence missing")
    start = swipe_data.get("start")
    end = swipe_data.get("end")
    _require(
        isinstance(start, dict)
        and isinstance(end, dict)
        and isinstance(start.get("x"), int)
        and isinstance(start.get("y"), int)
        and isinstance(end.get("x"), int)
        and isinstance(end.get("y"), int),
        "percentage_swipe",
        "swipe did not return native pixel coordinates",
    )

    moved = _success(
        await _call(
            client,
            "android_wait_for_text",
            {"text": "Scroll position: moved", "timeout_seconds": 8, "poll_interval_seconds": 0.25},
            timeout_seconds=stage_timeout_seconds,
            stage="wait_scroll",
        ),
        "wait_scroll",
    )
    moved_revision = _assert_provenance(moved, session_id, generation, "wait_scroll")
    _require(moved_revision > baseline_revision, "wait_scroll", "screen revision did not advance")

    stale_target = _find_element(
        scroll_page,
        FIXTURE_IDS["scroll_container"],
        "stale_target",
    )
    stale_result = await _call(
        client,
        "android_tap",
        {
            "target": {
                "element_ref": stale_target.get("element_ref"),
                "snapshot_id": baseline_id,
            }
        },
        timeout_seconds=stage_timeout_seconds,
        stage="stale_target",
    )
    _failure(stale_result, "stale_target", "ELEMENT_REF_STALE")

    current = _success(
        await _call(
            client,
            "android_snapshot",
            {"max_elements": 100, "image_format": "png"},
            timeout_seconds=stage_timeout_seconds,
            stage="current_snapshot",
        ),
        "current_snapshot",
    )
    current_id = current.get("snapshot_id")
    _require(
        isinstance(current_id, str) and current_id,
        "current_snapshot",
        "current snapshot ID is missing",
    )
    current_revision = _assert_provenance(current, session_id, generation, "current_snapshot")
    _require(
        current_revision >= moved_revision,
        "current_snapshot",
        "fresh observation regressed the screen revision",
    )
    _require(
        current_id != moved.get("snapshot_id"),
        "current_snapshot",
        "fresh observation reused the previous snapshot ID",
    )

    change_page = _success(
        await _call(
            client,
            "android_get_ui_elements",
            {"snapshot_id": current_id, "query": "fixture_change_button", "limit": 5},
            timeout_seconds=stage_timeout_seconds,
            stage="change_target",
        ),
        "change_target",
    )
    change_element = _find_element(change_page, FIXTURE_IDS["change_button"], "change_target")
    element_ref = change_element.get("element_ref")
    _require(
        isinstance(element_ref, str) and element_ref,
        "change_target",
        "change target has no element ref",
    )

    changed_result = await _call(
        client,
        "android_tap",
        {"target": {"element_ref": element_ref, "snapshot_id": current_id}},
        timeout_seconds=stage_timeout_seconds,
        stage="deterministic_change",
    )
    changed = _success(changed_result, "deterministic_change")
    _require(
        changed.get("command_status") == "sent", "deterministic_change", "tap command was not sent"
    )
    _require(
        changed.get("effect_status") == "unverified",
        "deterministic_change",
        "tap effect was incorrectly inferred",
    )
    change_wait = _success(
        await _call(
            client,
            "android_wait_for_ui_change",
            {
                "baseline_snapshot_id": current_id,
                "timeout_seconds": 10,
                "poll_interval_seconds": 0.25,
            },
            timeout_seconds=max(stage_timeout_seconds, 30),
            stage="wait_ui_change",
        ),
        "wait_ui_change",
    )
    changed_revision = _assert_provenance(change_wait, session_id, generation, "wait_ui_change")
    _require(
        changed_revision > current_revision, "wait_ui_change", "UI change revision did not advance"
    )

    changed_text = _success(
        await _call(
            client,
            "android_get_ui_elements",
            {"query": "fixture_change_value", "limit": 5},
            timeout_seconds=stage_timeout_seconds,
            stage="verify_ui_change",
        ),
        "verify_ui_change",
    )
    _require(
        _find_text(changed_text, FIXTURE_IDS["change_value"], "verify_ui_change")
        == "UI state: changed",
        "verify_ui_change",
        "fixture state did not change deterministically",
    )

    delayed_trigger = _success(
        await _call(
            client,
            "android_tap",
            {"target": _resource_target(FIXTURE_IDS["delayed_trigger"])},
            timeout_seconds=stage_timeout_seconds,
            stage="delayed_trigger",
        ),
        "delayed_trigger",
    )
    _require(
        delayed_trigger.get("command_status") == "sent",
        "delayed_trigger",
        "delayed trigger was not sent",
    )
    delayed = _success(
        await _call(
            client,
            "android_wait_for_element",
            {
                "target": _resource_target(FIXTURE_IDS["delayed_element"]),
                "timeout_seconds": 10,
                "poll_interval_seconds": 0.25,
            },
            timeout_seconds=max(stage_timeout_seconds, 30),
            stage="wait_delayed_element",
        ),
        "wait_delayed_element",
    )
    _assert_provenance(delayed, session_id, generation, "wait_delayed_element")
    matched = delayed.get("matched_element")
    _require(
        isinstance(matched, dict), "wait_delayed_element", "wait result has no matched element"
    )
    _require(
        matched.get("resource_id") == FIXTURE_IDS["delayed_element"],
        "wait_delayed_element",
        "wait matched a different element",
    )

    secret = "MCP 你好 ✓ café — 42"
    typed_result = await _call(
        client,
        "android_type_text",
        {"text": secret, "target": _resource_target(FIXTURE_IDS["text_input"])},
        timeout_seconds=stage_timeout_seconds,
        stage="type_unicode",
    )
    typed = _success(typed_result, "type_unicode")
    _require(typed.get("command_status") == "sent", "type_unicode", "input command was not sent")
    _require(
        typed.get("effect_status") == "verified", "type_unicode", "input effect was not verified"
    )
    _require(
        secret not in str(typed_result.content),
        "type_unicode",
        "typed text was echoed in MCP content",
    )
    _require(
        secret not in str(typed), "type_unicode", "typed text was echoed in structured metadata"
    )

    typed_page = _success(
        await _call(
            client,
            "android_get_ui_elements",
            {"query": "fixture_text_input", "limit": 5},
            timeout_seconds=stage_timeout_seconds,
            stage="verify_unicode",
        ),
        "verify_unicode",
    )
    _require(
        _find_text(typed_page, FIXTURE_IDS["text_input"], "verify_unicode") == secret,
        "verify_unicode",
        "fixture input did not contain the typed Unicode value",
    )

    cleared_result = await _call(
        client,
        "android_clear_text",
        {"target": _resource_target(FIXTURE_IDS["text_input"])},
        timeout_seconds=stage_timeout_seconds,
        stage="clear_unicode",
    )
    cleared = _success(cleared_result, "clear_unicode")
    _require(cleared.get("command_status") == "sent", "clear_unicode", "clear command was not sent")
    _require(
        cleared.get("effect_status") == "verified", "clear_unicode", "clear effect was not verified"
    )
    cleared_page = _success(
        await _call(
            client,
            "android_get_ui_elements",
            {"query": "fixture_text_input", "limit": 5},
            timeout_seconds=stage_timeout_seconds,
            stage="verify_clear",
        ),
        "verify_clear",
    )
    cleared_text = _find_text(cleared_page, FIXTURE_IDS["text_input"], "verify_clear")
    _require(cleared_text in (None, ""), "verify_clear", "fixture input was not empty after clear")


async def run_acceptance(
    *,
    serial: str,
    server_command: str,
    server_args: Sequence[str] = (),
    server_cwd: Path | None = None,
    package: str = FIXTURE_PACKAGE,
    stage_timeout_seconds: float = 35,
    total_timeout_seconds: float = 180,
) -> None:
    """Start an independent stdio server, run the flow, and always clean up."""

    _require(
        package == FIXTURE_PACKAGE,
        "fixture",
        "the stable fixture package cannot be overridden",
    )
    parameters = StdioServerParameters(
        command=server_command,
        args=list(server_args),
        cwd=str(server_cwd) if server_cwd is not None else None,
    )
    connect_attempted = False
    primary_error: BaseException | None = None
    cleanup_result: CallToolResult | None = None
    cleanup_reset_ok = False
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        try:
            await asyncio.wait_for(client.initialize(), timeout=stage_timeout_seconds)
            connect_attempted = True
            async with asyncio.timeout(total_timeout_seconds):
                await _workflow(
                    client,
                    serial=serial,
                    package=package,
                    stage_timeout_seconds=stage_timeout_seconds,
                )
        except BaseException as error:
            primary_error = error
            raise
        finally:
            cleanup_result, cleanup_reset_ok = await _cleanup_shielded(
                client,
                package=package,
                connect_attempted=connect_attempted,
                timeout_seconds=stage_timeout_seconds,
            )
            if primary_error is None:
                _require(
                    cleanup_reset_ok,
                    "cleanup.reset_fixture",
                    "fixture reset/Home cleanup did not complete successfully",
                )
                _require(
                    cleanup_result is not None
                    and cleanup_result.isError is not True
                    and isinstance(cleanup_result.structuredContent, dict)
                    and cleanup_result.structuredContent.get("success") is True,
                    "cleanup.disconnect",
                    "disconnect did not complete successfully",
                )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--serial",
        required=True,
        help="Exact authorized ADB serial; never inferred from device discovery.",
    )
    parser.add_argument(
        "--server-command",
        default="mobile-use-mcp",
        help="Installed MCP server executable (default: mobile-use-mcp).",
    )
    parser.add_argument(
        "--server-arg",
        action="append",
        default=[],
        help="Argument passed to the server; may be repeated.",
    )
    parser.add_argument(
        "--server-cwd",
        type=Path,
        default=None,
        help="Optional working directory for the independently started server.",
    )
    parser.add_argument(
        "--stage-timeout-seconds",
        type=float,
        default=35,
        help="Deadline for one MCP stage (default: 35).",
    )
    parser.add_argument(
        "--total-timeout-seconds",
        type=float,
        default=180,
        help="Deadline for the complete flow (default: 180).",
    )
    return parser


def _first_exception(error: BaseExceptionGroup) -> BaseException:
    """Unwrap the exception group emitted when stdio cleanup has one failure."""

    for nested in error.exceptions:
        if isinstance(nested, BaseExceptionGroup):
            return _first_exception(nested)
        return nested
    return error


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.stage_timeout_seconds <= 0 or args.total_timeout_seconds <= 0:
        print("android fixture acceptance failed: timeouts must be positive", file=sys.stderr)
        return 2
    try:
        asyncio.run(
            run_acceptance(
                serial=args.serial,
                server_command=args.server_command,
                server_args=args.server_arg,
                server_cwd=args.server_cwd,
                stage_timeout_seconds=args.stage_timeout_seconds,
                total_timeout_seconds=args.total_timeout_seconds,
            )
        )
    except AcceptanceError as error:
        print(f"android fixture acceptance failed: {error}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, TimeoutError) as error:
        # Do not print exception text from an adapter/server: it may contain
        # host paths, serials, or environment values.
        del error
        print(
            "android fixture acceptance failed: server or prerequisite unavailable", file=sys.stderr
        )
        return 1
    except BaseExceptionGroup as error:
        root_error = _first_exception(error)
        if isinstance(root_error, AcceptanceError):
            print(f"android fixture acceptance failed: {root_error}", file=sys.stderr)
        else:
            print(
                "android fixture acceptance failed: server or prerequisite unavailable",
                file=sys.stderr,
            )
        return 1
    except KeyboardInterrupt:
        print("android fixture acceptance cancelled", file=sys.stderr)
        return 130
    print("android fixture acceptance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
