"""MCP tool registration smoke tests."""

from __future__ import annotations

import pytest

from fetch_url_raw.server import fetch_url_raw, mcp


@pytest.mark.asyncio
async def test_tool_registered():
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert "fetch_url_raw" in names


@pytest.mark.asyncio
async def test_tool_callable_invalid_url():
    result = await fetch_url_raw(url="ftp://x")
    assert result["success"] is False
    assert result["error"]["type"] == "INVALID_URL"
