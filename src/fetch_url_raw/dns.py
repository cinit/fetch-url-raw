"""DNS override network backend for httpcore/httpx."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from typing import Any

import httpcore
from httpcore._backends.anyio import AnyIOBackend


class DnsOverrideBackend(AnyIOBackend):
    """Async network backend that remaps hostnames before TCP connect.

    Equivalent in spirit to ``curl --resolve host:port:ip``: TLS SNI and the
    HTTP Host header keep the original hostname; only the TCP destination IP
    changes.
    """

    def __init__(self, overrides: Mapping[str, str] | None = None) -> None:
        super().__init__()
        self._overrides: dict[str, str] = {}
        if overrides:
            for host, ip in overrides.items():
                self._overrides[host.lower().rstrip(".")] = str(ip).strip()

    def resolve_host(self, host: str) -> str:
        key = host.lower().rstrip(".")
        return self._overrides.get(key, host)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        connect_host = self.resolve_host(host)
        # Validate override is a literal address when provided; leave real DNS
        # to anyio for non-overridden hosts.
        if connect_host != host:
            try:
                ipaddress.ip_address(connect_host)
            except ValueError as exc:
                raise httpcore.ConnectError(
                    f"dns_override for {host!r} is not a valid IP address: {connect_host!r}"
                ) from exc
        return await super().connect_tcp(
            host=connect_host,
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
