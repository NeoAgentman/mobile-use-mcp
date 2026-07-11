import sys
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_stdio_server_initialize_list_and_call() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mobile_use_mcp"],
        cwd=project_root,
    )

    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        initialized = await client.initialize()
        tools = await client.list_tools()
        status = await client.call_tool(
            "android_status",
            read_timeout_seconds=timedelta(seconds=5),
        )

    assert initialized.serverInfo.name == "mobile_use_mcp"
    assert "android_snapshot" in {tool.name for tool in tools.tools}
    snapshot_tool = next(tool for tool in tools.tools if tool.name == "android_snapshot")
    screenshot_tool = next(tool for tool in tools.tools if tool.name == "android_screenshot")
    for tool in (snapshot_tool, screenshot_tool):
        properties = tool.inputSchema["properties"]
        assert "max_width" not in properties
        assert properties["image_format"]["default"] == "jpeg"  # type: ignore[index]
        assert properties["image_quality"]["default"] == 60  # type: ignore[index]
        assert "lossless" in str(properties["image_format"]).lower()
    assert status.isError is False
    assert status.structuredContent is not None
    assert status.structuredContent["connected"] is False


async def test_stdio_server_returns_actionable_adb_failure() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mobile_use_mcp"],
        cwd=project_root,
        env={"PATH": ""},
    )

    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        result = await client.call_tool(
            "android_list_devices",
            read_timeout_seconds=timedelta(seconds=5),
        )

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["success"] is False
    assert result.structuredContent["error_code"] == "ADB_UNAVAILABLE"


async def test_stdio_action_tool_validates_nested_target_and_returns_session_error() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mobile_use_mcp"],
        cwd=project_root,
    )

    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        result = await client.call_tool(
            "android_tap",
            arguments={"target": {"text": "Continue"}},
            read_timeout_seconds=timedelta(seconds=5),
        )

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["success"] is False
    assert result.structuredContent["error_code"] == "NOT_CONNECTED"
