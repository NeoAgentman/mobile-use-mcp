from unittest.mock import AsyncMock

import pytest

from mobile_use_mcp.server import mcp, server_lifespan, session


def test_server_name() -> None:
    assert mcp.name == "mobile_use_mcp"


@pytest.mark.asyncio
async def test_server_lifespan_closes_owned_session(monkeypatch: pytest.MonkeyPatch) -> None:
    close = AsyncMock()
    monkeypatch.setattr(session, "close", close)

    async with server_lifespan(None):
        pass

    close.assert_awaited_once()
