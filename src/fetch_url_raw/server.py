"""MCP server entrypoint exposing the fetch_url_raw tool."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from fetch_url_raw.fetch import fetch_url_raw as do_fetch

mcp = FastMCP(
    name="fetch-url-raw",
    instructions=(
        "Stateless HTTP client tool. Use fetch_url_raw to perform arbitrary "
        "HTTP requests with custom methods, headers, body, timeouts, redirect "
        "control, response size limits, and optional DNS overrides. No cookies "
        "or session state are retained between calls."
    ),
)


@mcp.tool(
    name="fetch_url_raw",
    description=(
        "Perform a single raw HTTP request and return status, headers, and body. "
        "Supports custom method/headers/body, timeouts, redirect following, "
        "max response size, TLS verify toggle, and DNS overrides (like curl --resolve). "
        "Text-like bodies are returned as UTF-8 text; binary bodies are base64-encoded. "
        "On failure returns {success:false, error:{type,message}} instead of throwing."
    ),
    structured_output=True,
)
async def fetch_url_raw(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout: float = 30.0,
    follow_redirect: bool = True,
    max_response_bytes: int = 1_048_576,
    dns_override: dict[str, str] | None = None,
    verify_tls: bool = True,
) -> dict[str, Any]:
    """Fetch a URL with raw HTTP semantics.

    Args:
        url: Absolute http(s) URL to request.
        method: HTTP method (default GET). Normalized to uppercase.
        headers: Optional request headers (string to string).
        body: Optional raw request body string (no automatic JSON encoding).
        timeout: Total request timeout in seconds (default 30).
        follow_redirect: Whether to follow redirects (default true).
        max_response_bytes: Stop reading body after this many bytes (default 1 MiB).
        dns_override: Optional map of hostname -> IP for connection only (SNI/Host preserved).
        verify_tls: Verify TLS certificates (default true).

    Returns:
        Structured success payload or {success:false, error:{type,message}}.
    """
    return await do_fetch(
        url=url,
        method=method,
        headers=headers,
        body=body,
        timeout=timeout,
        follow_redirect=follow_redirect,
        max_response_bytes=max_response_bytes,
        dns_override=dns_override,
        verify_tls=verify_tls,
    )


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
