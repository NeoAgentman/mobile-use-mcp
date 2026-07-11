from mobile_use_mcp.server import mcp


def test_server_name() -> None:
    assert mcp.name == "mobile_use_mcp"
