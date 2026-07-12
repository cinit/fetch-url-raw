"""Tests for DNS override backend."""

from __future__ import annotations

import httpcore
import pytest

from fetch_url_raw.dns import DnsOverrideBackend


def test_resolve_host_case_insensitive():
    backend = DnsOverrideBackend({"Example.COM": "10.0.0.1"})
    assert backend.resolve_host("example.com") == "10.0.0.1"
    assert backend.resolve_host("EXAMPLE.COM.") == "10.0.0.1"
    assert backend.resolve_host("other.test") == "other.test"


@pytest.mark.asyncio
async def test_connect_rejects_non_ip_override(monkeypatch):
    backend = DnsOverrideBackend({"example.com": "not-an-ip"})
    # Bypass validation in _validate_dns_override by constructing directly
    backend._overrides["example.com"] = "not-an-ip"
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("example.com", 443, timeout=1.0)
