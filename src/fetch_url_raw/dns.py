"""Network backend: DNS override + post-resolve destination IP policy."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import anyio
import httpcore
from httpcore._backends.anyio import AnyIOBackend

from fetch_url_raw.network_policy import blocked_reason, is_destination_blocked


class GuardedNetworkBackend(AnyIOBackend):
    """Async network backend with optional DNS remap and IP destination policy.

    * DNS is never filtered by name — only the final TCP destination IP is checked.
    * Connections are pinned to a checked IP (avoids DNS rebinding TOCTOU).
    * TLS SNI remains the original request hostname (set by httpcore from the origin).
    """

    def __init__(
        self,
        overrides: Mapping[str, str] | None = None,
        *,
        allow_private_network: bool = False,
    ) -> None:
        super().__init__()
        self._allow_private_network = allow_private_network
        self._overrides: dict[str, str] = {}
        if overrides:
            for host, ip in overrides.items():
                self._overrides[host.lower().rstrip(".")] = str(ip).strip()

    def resolve_override(self, host: str) -> str | None:
        key = host.lower().rstrip(".")
        return self._overrides.get(key)

    def _ensure_allowed(self, ip: str) -> None:
        reason = blocked_reason(ip, allow_private_network=self._allow_private_network)
        if reason is not None:
            raise httpcore.ConnectError(f"DESTINATION_BLOCKED: {reason}")

    async def _resolve_candidate_ips(self, host: str) -> list[str]:
        """Resolve host to candidate IPs, applying dns_override when present."""
        override = self.resolve_override(host)
        if override is not None:
            try:
                ipaddress.ip_address(override)
            except ValueError as exc:
                raise httpcore.ConnectError(
                    f"dns_override for {host!r} is not a valid IP address: {override!r}"
                ) from exc
            return [override]

        # Literal IP host (URL or already remapped).
        try:
            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            pass

        try:
            infos: Sequence[tuple[Any, ...]] = await anyio.getaddrinfo(
                host,
                None,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise httpcore.ConnectError(f"Name or service not known: {host}") from exc

        ips: list[str] = []
        seen: set[str] = set()
        for info in infos:
            sockaddr = info[4]
            if not sockaddr:
                continue
            ip = sockaddr[0]
            if ip not in seen:
                seen.add(ip)
                ips.append(ip)
        if not ips:
            raise httpcore.ConnectError(f"Name or service not known: {host}")
        return ips

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        candidates = await self._resolve_candidate_ips(host)

        allowed_ips = [
            ip
            for ip in candidates
            if not is_destination_blocked(ip, allow_private_network=self._allow_private_network)
        ]
        if not allowed_ips:
            # Prefer a specific reason from the first candidate.
            self._ensure_allowed(candidates[0])
            # _ensure_allowed always raises when blocked; keep fallback for safety.
            raise httpcore.ConnectError(
                f"DESTINATION_BLOCKED: no permitted addresses for host {host!r}"
            )

        errors: list[str] = []
        for ip in allowed_ips:
            try:
                # Pin connection to the checked IP. SNI/Host stay on original hostname.
                return await super().connect_tcp(
                    host=ip,
                    port=port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectError as exc:
                errors.append(f"{ip}: {exc}")
                continue
            except OSError as exc:
                errors.append(f"{ip}: {exc}")
                continue

        detail = "; ".join(errors) if errors else "unknown error"
        raise httpcore.ConnectError(
            f"All connection attempts failed for {host!r} ({detail})"
        )


# Back-compat alias used by older imports/tests.
DnsOverrideBackend = GuardedNetworkBackend
