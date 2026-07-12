"""Destination IP policy for SSRF-oriented private-network blocking.

Policy is applied to **resolved IPs** (and literal IP hosts / dns_override
targets), never by hostname string. DNS is always allowed; only the TCP
destination address is checked.
"""

from __future__ import annotations

import ipaddress
from typing import Iterable

# Always denied even when private/local destinations are allowed.
ALWAYS_BLOCKED_NETWORKS: tuple[ipaddress._BaseNetwork, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),  # "this" network / unspecified-ish
    ipaddress.ip_network("224.0.0.0/4"),  # multicast
    ipaddress.ip_network("240.0.0.0/4"),  # reserved
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("::/128"),  # unspecified
    ipaddress.ip_network("ff00::/8"),  # multicast
)

# Blocked by default; permitted when allow_private_network=True.
# Includes RFC1918, loopback, link-local, CGNAT, IPv6 ULA (fc00::/7),
# and IPv6 link-local (fe80::/10). Note: standard link-local is fe80::/10,
# not fe00::/10.
PRIVATE_OR_LOCAL_NETWORKS: tuple[ipaddress._BaseNetwork, ...] = (
    # RFC1918
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    # Loopback
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    # Link-local (includes cloud metadata 169.254.169.254)
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    # Carrier-grade NAT
    ipaddress.ip_network("100.64.0.0/10"),
    # IPv6 unique local addresses
    ipaddress.ip_network("fc00::/7"),
)


def _normalize_ip(value: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    addr = ipaddress.ip_address(value)
    # Evaluate IPv4-mapped IPv6 (::ffff:a.b.c.d) against IPv4 rules.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _in_networks(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: Iterable[ipaddress._BaseNetwork],
) -> bool:
    for network in networks:
        try:
            if addr in network:
                return True
        except TypeError:
            # IPv4 address vs IPv6 network (or reverse): skip.
            continue
    return False


def is_destination_blocked(ip: str, *, allow_private_network: bool = False) -> bool:
    """Return True if connecting to this IP should be refused."""
    try:
        addr = _normalize_ip(ip)
    except ValueError:
        # Not a literal IP — caller should resolve first.
        return False

    if _in_networks(addr, ALWAYS_BLOCKED_NETWORKS):
        return True
    if not allow_private_network and _in_networks(addr, PRIVATE_OR_LOCAL_NETWORKS):
        return True
    return False


def blocked_reason(ip: str, *, allow_private_network: bool = False) -> str | None:
    """Human-readable block reason, or None if allowed."""
    try:
        addr = _normalize_ip(ip)
    except ValueError:
        return None

    if _in_networks(addr, ALWAYS_BLOCKED_NETWORKS):
        return f"destination IP {addr} is in a blocked special-use range"
    if not allow_private_network and _in_networks(addr, PRIVATE_OR_LOCAL_NETWORKS):
        return (
            f"destination IP {addr} is private/local "
            f"(use --allow-private-network to permit RFC1918/ULA/link-local/loopback)"
        )
    return None
