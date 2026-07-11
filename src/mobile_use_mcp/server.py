"""stdio MCP server entry point."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mobile_use_mcp")


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
