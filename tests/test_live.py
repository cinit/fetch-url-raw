"""Optional live network tests (skipped offline / when env not set)."""

from __future__ import annotations

import os

import pytest

from fetch_url_raw.fetch import fetch_url_raw

pytestmark = pytest.mark.skipif(
    os.environ.get("FETCH_URL_RAW_LIVE") != "1",
    reason="Set FETCH_URL_RAW_LIVE=1 to run live network tests",
)


@pytest.mark.asyncio
async def test_live_example_com():
    result = await fetch_url_raw(url="https://example.com/", timeout=15)
    assert result["success"] is True
    assert result["status"] == 200
    assert result["body"] is not None
    assert "Example Domain" in result["body"] or result["received_bytes"] > 0
