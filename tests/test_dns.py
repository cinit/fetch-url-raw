"""Tests for DNS override / guarded network backend."""

from __future__ import annotations

import httpcore
import pytest

from fetch_url_raw.dns import DnsOverrideBackend, GuardedNetworkBackend


def test_resolve_override_case_insensitive():
    backend = GuardedNetworkBackend({"Example.COM": "8.8.8.8"}, allow_private_network=True)
    assert backend.resolve_override("example.com") == "8.8.8.8"
    assert backend.resolve_override("EXAMPLE.COM.") == "8.8.8.8"
    assert backend.resolve_override("other.test") is None


@pytest.mark.asyncio
async def test_connect_rejects_non_ip_override():
    backend = GuardedNetworkBackend({"example.com": "not-an-ip"}, allow_private_network=True)
    backend._overrides["example.com"] = "not-an-ip"
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("example.com", 443, timeout=1.0)


def test_backcompat_alias():
    assert DnsOverrideBackend is GuardedNetworkBackend
