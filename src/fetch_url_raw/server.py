"""MCP server entrypoint exposing the fetch_url_raw tool."""

from __future__ import annotations

import argparse
import sys
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from fetch_url_raw.fetch import fetch_url_raw as do_fetch

TransportName = Literal["stdio", "streamable-http", "sse"]

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetch-url-raw",
        description=(
            "Stateless MCP server for the fetch_url_raw HTTP tool. "
            "Default transport is stdio. HTTP modes are optional and must be "
            "enabled explicitly with --transport."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default="stdio",
        help="MCP transport (default: stdio). Use streamable-http or sse for HTTP server mode.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Listen address for HTTP transports (default: 127.0.0.1). Ignored for stdio.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Listen port for HTTP transports (default: 8000). Ignored for stdio.",
    )
    parser.add_argument(
        "--path",
        default=None,
        help=(
            "URL path for the HTTP transport endpoint "
            "(streamable-http default: /mcp, sse default: /sse). Ignored for stdio."
        ),
    )
    parser.add_argument(
        "--stateless-http",
        action="store_true",
        help="Enable FastMCP stateless HTTP mode (streamable-http only).",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help=(
            "Relax DNS-rebinding host/origin checks for non-localhost HTTP binds. "
            "Only use on trusted networks."
        ),
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default="INFO",
        help="Log level for HTTP server mode (default: INFO).",
    )
    return parser


def _validate_bind_args(host: str, port: int) -> None:
    if not host or not str(host).strip():
        raise SystemExit("error: --host must be a non-empty address")
    if not isinstance(port, int) or port < 1 or port > 65535:
        raise SystemExit("error: --port must be an integer in 1..65535")


def configure_http_settings(
    *,
    host: str,
    port: int,
    transport: TransportName,
    path: str | None,
    stateless_http: bool,
    allow_remote: bool,
    log_level: str,
) -> None:
    """Apply listen address and security settings for HTTP transports."""
    _validate_bind_args(host, port)

    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.log_level = log_level
    mcp.settings.stateless_http = bool(stateless_http)

    if path:
        if not path.startswith("/"):
            raise SystemExit("error: --path must start with '/'")
        if transport == "streamable-http":
            mcp.settings.streamable_http_path = path
        elif transport == "sse":
            mcp.settings.sse_path = path

    host_is_local = host in {"127.0.0.1", "localhost", "::1"}
    if allow_remote or not host_is_local:
        # Non-loopback binds need broader Host/Origin allowances or clients
        # cannot reach the MCP HTTP endpoint. Prefer explicit --allow-remote.
        if not host_is_local and not allow_remote:
            print(
                "warning: binding to non-loopback host without --allow-remote; "
                "enabling relaxed transport security so remote clients can connect. "
                "Pass --allow-remote to acknowledge this explicitly in future.",
                file=sys.stderr,
            )
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )
    else:
        # Keep default rebinding protection for localhost.
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{host}:*", "localhost:*", "127.0.0.1:*", "[::1]:*"],
            allowed_origins=[
                f"http://{host}:*",
                "http://localhost:*",
                "http://127.0.0.1:*",
                "http://[::1]:*",
            ],
        )


def main(argv: list[str] | None = None) -> None:
    """CLI entry: stdio by default; optional HTTP server via --transport."""
    parser = build_parser()
    args = parser.parse_args(argv)
    transport: TransportName = args.transport

    if transport == "stdio":
        if args.stateless_http:
            print("warning: --stateless-http is ignored for stdio transport", file=sys.stderr)
        if args.path is not None:
            print("warning: --path is ignored for stdio transport", file=sys.stderr)
        mcp.run(transport="stdio")
        return

    configure_http_settings(
        host=args.host,
        port=args.port,
        transport=transport,
        path=args.path,
        stateless_http=args.stateless_http,
        allow_remote=args.allow_remote,
        log_level=args.log_level,
    )

    endpoint = (
        mcp.settings.streamable_http_path
        if transport == "streamable-http"
        else mcp.settings.sse_path
    )
    display_host = args.host if args.host != "0.0.0.0" else "127.0.0.1"
    print(
        f"Starting fetch-url-raw MCP HTTP server "
        f"transport={transport} listen={args.host}:{args.port} path={endpoint} "
        f"(example: http://{display_host}:{args.port}{endpoint})",
        file=sys.stderr,
    )
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
