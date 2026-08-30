from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mobile_use_mcp.server import mcp

PUBLIC_ANDROID_TOOLS = {
    "android_list_devices",
    "android_doctor",
    "android_connect",
    "android_status",
    "android_disconnect",
    "android_snapshot",
    "android_screenshot",
    "android_get_ui_elements",
    "android_get_foreground_app",
    "android_list_apps",
    "android_tap",
    "android_long_press",
    "android_swipe",
    "android_type_text",
    "android_clear_text",
    "android_press_key",
    "android_launch_app",
    "android_terminate_app",
    "android_open_url",
    "android_start_recording",
    "android_stop_recording",
    "android_recording_status",
    "android_retrieve_recording",
    "android_wait",
    "android_wait_for_text",
    "android_wait_for_element",
    "android_wait_for_ui_change",
}


# Keep this table deliberately boring: it exercises the real stdio transport
# with one safe, schema-valid call for every public tool while ADB is hidden.
# The contract test therefore does not depend on a physical device or on an
# installed app, but still observes the same MCP ``isError`` and
# ``structuredContent`` boundary as a host agent.
STDIO_CALLS = {
    "android_list_devices": {},
    "android_doctor": {},
    "android_connect": {"serial": "__typed_contract_missing_device__"},
    "android_status": {},
    "android_disconnect": {},
    "android_snapshot": {},
    "android_screenshot": {},
    "android_get_ui_elements": {},
    "android_get_foreground_app": {},
    "android_list_apps": {},
    "android_tap": {"target": {"text": "Continue"}},
    "android_long_press": {"target": {"text": "Continue"}},
    "android_swipe": {"start_x": 1, "start_y": 1, "end_x": 2, "end_y": 2},
    "android_type_text": {"text": "typed-contract-secret"},
    "android_clear_text": {},
    "android_press_key": {"key": "back"},
    "android_launch_app": {"package": "com.example.missing"},
    "android_terminate_app": {"package": "com.example.missing"},
    "android_open_url": {"url": "https://example.com"},
    "android_start_recording": {"max_duration_seconds": 5},
    "android_stop_recording": {},
    "android_recording_status": {},
    "android_retrieve_recording": {"artifact_id": "artifact-" + "0" * 32},
    "android_wait": {"milliseconds": 0},
    "android_wait_for_text": {
        "text": "typed-contract-secret",
        "timeout_seconds": 0.01,
        "poll_interval_seconds": 0.01,
    },
    "android_wait_for_element": {
        "target": {"text": "Continue"},
        "timeout_seconds": 0.01,
        "poll_interval_seconds": 0.01,
    },
    "android_wait_for_ui_change": {
        "baseline_screen_revision": 0,
        "timeout_seconds": 0.01,
        "poll_interval_seconds": 0.01,
    },
}

READ_ONLY_TOOLS = {
    "android_doctor",
    "android_get_foreground_app",
    "android_get_ui_elements",
    "android_list_apps",
    "android_list_devices",
    "android_recording_status",
    "android_screenshot",
    "android_snapshot",
    "android_status",
    "android_wait",
    "android_wait_for_element",
    "android_wait_for_text",
    "android_wait_for_ui_change",
}

MUTATING_OPEN_WORLD_TOOLS = {
    "android_clear_text",
    "android_launch_app",
    "android_long_press",
    "android_open_url",
    "android_press_key",
    "android_retrieve_recording",
    "android_start_recording",
    "android_stop_recording",
    "android_swipe",
    "android_tap",
    "android_terminate_app",
    "android_type_text",
}


async def test_real_stdio_contract_covers_every_public_tool() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mobile_use_mcp"],
        cwd=project_root,
        env={**os.environ, "PATH": "", "MOBILE_USE_ADB_EXECUTABLE": "__missing_adb__"},
    )

    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        listed = await client.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        assert set(tools) == PUBLIC_ANDROID_TOOLS
        revision_before_wait: int | None = None

        for name, arguments in STDIO_CALLS.items():
            tool = tools[name]
            assert tool.outputSchema is not None
            assert tool.annotations is not None
            assert tool.annotations.readOnlyHint is (name in READ_ONLY_TOOLS)
            if name in MUTATING_OPEN_WORLD_TOOLS:
                assert tool.annotations.destructiveHint is True
                assert tool.annotations.openWorldHint is True
                assert tool.annotations.idempotentHint is False
            elif name == "android_connect":
                assert tool.annotations.destructiveHint is False
                assert tool.annotations.openWorldHint is False
                assert tool.annotations.idempotentHint is False
            elif name == "android_disconnect":
                assert tool.annotations.destructiveHint is False
                assert tool.annotations.openWorldHint is False
                assert tool.annotations.idempotentHint is True
            else:
                assert tool.annotations.destructiveHint is False
                assert tool.annotations.openWorldHint is False
                assert tool.annotations.idempotentHint is True
            if name == "android_wait":
                before_wait = await client.call_tool(
                    "android_status",
                    read_timeout_seconds=timedelta(seconds=5),
                )
                assert before_wait.structuredContent is not None
                revision_before_wait = before_wait.structuredContent["screen_revision"]
            result = await client.call_tool(
                name,
                arguments=arguments,
                read_timeout_seconds=timedelta(seconds=5),
            )
            assert result.structuredContent is not None, name
            payload = result.structuredContent
            assert payload["operation_id"], name
            assert "warnings" in payload, name
            if result.isError:
                assert payload["success"] is False, name
                assert {
                    "error_code",
                    "category",
                    "retryable",
                    "message",
                    "suggestion",
                    "details",
                } <= set(payload), name
            elif name != "android_doctor":
                assert payload["success"] is True, name

            if name == "android_type_text":
                assert "typed-contract-secret" not in str(result.content)
                assert "typed-contract-secret" not in str(payload)

        after = await client.call_tool(
            "android_status",
            read_timeout_seconds=timedelta(seconds=5),
        )
        assert after.structuredContent is not None
        # ``android_wait`` clears the cached snapshot and advances the
        # session revision even when no device is connected.
        assert revision_before_wait is not None
        assert after.structuredContent["screen_revision"] == revision_before_wait + 1


async def test_every_public_android_tool_has_an_explicit_output_schema() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}

    assert tools.keys() >= PUBLIC_ANDROID_TOOLS
    for name in PUBLIC_ANDROID_TOOLS:
        assert tools[name].outputSchema is not None
        assert {
            "success",
            "operation_id",
            "message",
            "warnings",
            "error_code",
            "category",
            "retryable",
            "suggestion",
            "details",
        } <= set(tools[name].outputSchema["properties"])  # type: ignore[index]
    assert "operation_id" in tools["android_snapshot"].outputSchema["properties"]  # type: ignore[index]
    assert "operation_id" in tools["android_screenshot"].outputSchema["properties"]  # type: ignore[index]
    assert "data" in tools["android_tap"].outputSchema["properties"]  # type: ignore[index]
    assert "data" in tools["android_type_text"].outputSchema["properties"]  # type: ignore[index]

    defaults = {
        ("android_snapshot", "detail_level"): "compact",
        ("android_snapshot", "max_elements"): 200,
        ("android_screenshot", "image_format"): "jpeg",
        ("android_screenshot", "image_quality"): 60,
        ("android_wait", "milliseconds"): 1_000,
    }
    for (name, field), value in defaults.items():
        assert tools[name].inputSchema["properties"][field]["default"] == value  # type: ignore[index]


async def test_mutating_android_tools_are_annotated_as_open_world_destructive_actions() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    names = {
        "android_tap",
        "android_long_press",
        "android_swipe",
        "android_type_text",
        "android_clear_text",
        "android_press_key",
        "android_launch_app",
        "android_terminate_app",
        "android_open_url",
    }

    for name in names:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.readOnlyHint is False
        assert annotations.destructiveHint is True
        assert annotations.openWorldHint is True


async def test_read_only_tools_remain_idempotent_and_closed_world() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}

    for name in {"android_status", "android_snapshot", "android_screenshot", "android_wait"}:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.readOnlyHint is True
        assert annotations.idempotentHint is True
        assert annotations.openWorldHint is False
