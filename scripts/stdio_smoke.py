"""Exercise the installed stdio entry point and a safe business-error path."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
from datetime import timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mobile_use_mcp import __version__


async def run_smoke(command: str) -> None:
    """Start ``command`` and verify the public MCP discovery contract."""

    parameters = StdioServerParameters(command=command, args=[])
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        initialized = await asyncio.wait_for(client.initialize(), timeout=10)
        tools = await asyncio.wait_for(client.list_tools(), timeout=10)
        status = await asyncio.wait_for(
            client.call_tool(
                "android_status",
                read_timeout_seconds=timedelta(seconds=10),
            ),
            timeout=10,
        )
        safe_failure = await asyncio.wait_for(
            client.call_tool(
                "android_snapshot",
                read_timeout_seconds=timedelta(seconds=10),
            ),
            timeout=10,
        )

    if initialized.serverInfo.name != "mobile_use_mcp":
        raise RuntimeError(f"Unexpected MCP server name: {initialized.serverInfo.name!r}")
    if initialized.serverInfo.version != __version__:
        raise RuntimeError(
            f"MCP server version {initialized.serverInfo.version!r} does not match "
            f"installed package version {__version__!r}."
        )
    tool_names = {tool.name for tool in tools.tools}
    required_tools = {"android_list_devices", "android_snapshot", "android_tap"}
    missing_tools = sorted(required_tools - tool_names)
    if missing_tools:
        raise RuntimeError(f"Installed MCP server is missing tools: {', '.join(missing_tools)}")
    if status.isError or not isinstance(status.structuredContent, dict):
        raise RuntimeError("Installed MCP server returned an invalid android_status result")
    if status.structuredContent.get("connected") is not False:
        raise RuntimeError("Installed MCP server did not start in a disconnected state")
    if (
        safe_failure.isError is not True
        or not isinstance(safe_failure.structuredContent, dict)
        or safe_failure.structuredContent.get("success") is not False
        or safe_failure.structuredContent.get("error_code") != "NOT_CONNECTED"
    ):
        raise RuntimeError(
            "Installed MCP server did not expose the expected safe NOT_CONNECTED failure"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", default="mobile-use-mcp")
    args = parser.parse_args(argv)
    installed_version = importlib.metadata.version("mobile-use-mcp")
    if installed_version != __version__:
        raise SystemExit(
            f"Installed metadata version {installed_version!r} does not match runtime "
            f"version {__version__!r}."
        )
    asyncio.run(run_smoke(args.command))
    print(f"installed stdio smoke passed for mobile-use-mcp {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
