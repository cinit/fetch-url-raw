"""Process-level runtime configuration for the MCP server."""

from __future__ import annotations

# When False (default), resolved destination IPs in private/local ranges are blocked.
ALLOW_PRIVATE_NETWORK: bool = False


def configure(*, allow_private_network: bool | None = None) -> None:
    """Update process-level settings."""
    global ALLOW_PRIVATE_NETWORK
    if allow_private_network is not None:
        ALLOW_PRIVATE_NETWORK = bool(allow_private_network)


def get_allow_private_network() -> bool:
    return ALLOW_PRIVATE_NETWORK
