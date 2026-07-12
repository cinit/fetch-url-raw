"""Destination IP network policy tests."""

from __future__ import annotations

import httpcore
import httpx
import pytest

from fetch_url_raw.dns import GuardedNetworkBackend
from fetch_url_raw.fetch import fetch_url_raw
from fetch_url_raw.network_policy import blocked_reason, is_destination_blocked


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.1",
        "172.16.5.1",
        "192.168.1.10",
        "127.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "fc00::1",
        "fd12::ab",
        "fe80::1",
        "::1",
        "::ffff:192.168.0.1",
    ],
)
def test_private_blocked_by_default(ip: str):
    assert is_destination_blocked(ip, allow_private_network=False) is True
    assert blocked_reason(ip, allow_private_network=False) is not None


@pytest.mark.parametrize(
    "ip",
    [
        "8.8.8.8",
        "1.1.1.1",
        "2001:4860:4860::8888",
        "::ffff:8.8.8.8",
    ],
)
def test_public_allowed(ip: str):
    assert is_destination_blocked(ip, allow_private_network=False) is False
    assert is_destination_blocked(ip, allow_private_network=True) is False


@pytest.mark.parametrize(
    "ip",
    [
        "10.1.2.3",
        "192.168.0.1",
        "127.0.0.1",
        "fc00::1",
        "fe80::1",
    ],
)
def test_private_allowed_when_flag_set(ip: str):
    assert is_destination_blocked(ip, allow_private_network=True) is False


@pytest.mark.parametrize("ip", ["0.0.0.0", "224.0.0.1", "255.255.255.255", "::", "ff02::1"])
def test_always_blocked_special_use(ip: str):
    assert is_destination_blocked(ip, allow_private_network=True) is True
    assert is_destination_blocked(ip, allow_private_network=False) is True


@pytest.mark.asyncio
async def test_backend_blocks_override_to_private_ip():
    backend = GuardedNetworkBackend(
        {"example.com": "127.0.0.1"},
        allow_private_network=False,
    )
    with pytest.raises(httpcore.ConnectError) as ei:
        await backend.connect_tcp("example.com", 443, timeout=1.0)
    assert "DESTINATION_BLOCKED" in str(ei.value)


@pytest.mark.asyncio
async def test_backend_blocks_literal_private_ip():
    backend = GuardedNetworkBackend(allow_private_network=False)
    with pytest.raises(httpcore.ConnectError) as ei:
        await backend.connect_tcp("10.0.0.5", 80, timeout=1.0)
    assert "DESTINATION_BLOCKED" in str(ei.value)


@pytest.mark.asyncio
async def test_fetch_blocks_private_literal_url():
    result = await fetch_url_raw(
        url="http://127.0.0.1/",
        timeout=2,
        allow_private_network=False,
    )
    assert result["success"] is False
    assert result["error"]["type"] == "DESTINATION_BLOCKED"


@pytest.mark.asyncio
async def test_fetch_blocks_dns_override_to_private():
    result = await fetch_url_raw(
        url="https://example.com/",
        timeout=2,
        dns_override={"example.com": "192.168.1.1"},
        allow_private_network=False,
        verify_tls=False,
    )
    assert result["success"] is False
    assert result["error"]["type"] == "DESTINATION_BLOCKED"


@pytest.mark.asyncio
async def test_fetch_allows_private_when_enabled_connect_error_not_policy(monkeypatch):
    # With policy disabled, attempt may still fail to connect; must not be DESTINATION_BLOCKED.
    result = await fetch_url_raw(
        url="http://127.0.0.1:1/",
        timeout=1,
        allow_private_network=True,
    )
    assert result["success"] is False
    assert result["error"]["type"] != "DESTINATION_BLOCKED"
