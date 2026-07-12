"""Core HTTP fetch implementation for fetch_url_raw."""

from __future__ import annotations

import base64
import re
import time
from email.message import Message
from typing import Any

import httpcore
import httpx

from fetch_url_raw.dns import DnsOverrideBackend
from fetch_url_raw.errors import (
    ConnectError_,
    DnsError,
    FetchError,
    InvalidParameterError,
    InvalidUrlError,
    ProtocolError_,
    TimeoutError_,
    TlsError,
)

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
DEFAULT_MAX_REDIRECTS = 20
ALLOWED_SCHEMES = frozenset({"http", "https"})
# Methods commonly used by HTTP clients; TRACE is allowed per design but
# some environments may still block it downstream.
ALLOWED_METHODS = frozenset(
    {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE"}
)

_TEXTISH_CONTENT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/javascript",
    "application/xhtml+xml",
    "application/atom+xml",
    "application/rss+xml",
    "application/problem+json",
    "application/ld+json",
    "application/graphql",
    "application/sql",
    "application/x-www-form-urlencoded",
)


def _parse_content_type(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    msg = Message()
    msg["content-type"] = value
    content_type = msg.get_content_type()
    charset = msg.get_content_charset()
    return content_type, charset


def _is_text_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    ct = content_type.lower()
    return any(ct.startswith(prefix) or ct == prefix.rstrip("/") for prefix in _TEXTISH_CONTENT_TYPES)


def _headers_to_dict(headers: httpx.Headers) -> dict[str, str]:
    # Preserve multi-value headers by joining with ", " as HTTP commonly does.
    result: dict[str, str] = {}
    for key, value in headers.multi_items():
        if key in result:
            result[key] = f"{result[key]}, {value}"
        else:
            result[key] = value
    return result


def _validate_url(url: str) -> httpx.URL:
    if not isinstance(url, str) or not url.strip():
        raise InvalidUrlError("url must be a non-empty string")
    try:
        parsed = httpx.URL(url.strip())
    except Exception as exc:  # noqa: BLE001 - surface as INVALID_URL
        raise InvalidUrlError(f"Invalid URL: {exc}") from exc

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise InvalidUrlError(
            f"Unsupported URL scheme {parsed.scheme!r}; allowed: {sorted(ALLOWED_SCHEMES)}"
        )
    if not parsed.host:
        raise InvalidUrlError("URL must include a host")
    return parsed


def _validate_method(method: str | None) -> str:
    if method is None or method == "":
        return "GET"
    if not isinstance(method, str):
        raise InvalidParameterError("method must be a string")
    normalized = method.strip().upper()
    if not normalized or not re.fullmatch(r"[A-Z]+", normalized):
        raise InvalidParameterError(f"Invalid HTTP method: {method!r}")
    if normalized not in ALLOWED_METHODS:
        raise InvalidParameterError(
            f"Unsupported HTTP method {normalized!r}; allowed: {sorted(ALLOWED_METHODS)}"
        )
    return normalized


def _validate_headers(headers: dict[str, str] | None) -> dict[str, str] | None:
    if headers is None:
        return None
    if not isinstance(headers, dict):
        raise InvalidParameterError("headers must be an object of string to string")
    out: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise InvalidParameterError("headers keys and values must be strings")
        if "\r" in key or "\n" in key or "\r" in value or "\n" in value:
            raise InvalidParameterError("headers must not contain CR/LF characters")
        out[key] = value
    return out


def _validate_timeout(timeout: float | int | None) -> float:
    if timeout is None:
        return DEFAULT_TIMEOUT
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        raise InvalidParameterError("timeout must be a number (seconds)")
    if timeout <= 0:
        raise InvalidParameterError("timeout must be positive")
    if timeout > 600:
        raise InvalidParameterError("timeout must be <= 600 seconds")
    return float(timeout)


def _validate_max_response_bytes(value: int | None) -> int:
    if value is None:
        return DEFAULT_MAX_RESPONSE_BYTES
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidParameterError("max_response_bytes must be an integer")
    if value < 0:
        raise InvalidParameterError("max_response_bytes must be >= 0")
    if value > 50 * 1024 * 1024:
        raise InvalidParameterError("max_response_bytes must be <= 52428800 (50 MiB)")
    return value


def _validate_dns_override(dns_override: dict[str, str] | None) -> dict[str, str] | None:
    if dns_override is None:
        return None
    if not isinstance(dns_override, dict):
        raise InvalidParameterError("dns_override must be an object of hostname to IP")
    out: dict[str, str] = {}
    for host, ip in dns_override.items():
        if not isinstance(host, str) or not isinstance(ip, str):
            raise InvalidParameterError("dns_override keys and values must be strings")
        host_key = host.strip().lower().rstrip(".")
        ip_val = ip.strip()
        if not host_key:
            raise InvalidParameterError("dns_override hostname must be non-empty")
        # Validate IP format early
        try:
            import ipaddress

            ipaddress.ip_address(ip_val)
        except ValueError as exc:
            raise InvalidParameterError(
                f"dns_override value for {host!r} is not a valid IP address: {ip_val!r}"
            ) from exc
        out[host_key] = ip_val
    return out


def _decode_body(raw: bytes, content_type: str | None, charset: str | None) -> dict[str, Any]:
    if _is_text_content_type(content_type):
        encoding = charset or "utf-8"
        try:
            text = raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            try:
                text = raw.decode("utf-8")
                encoding = "utf-8"
            except UnicodeDecodeError:
                return {
                    "body": None,
                    "body_base64": base64.b64encode(raw).decode("ascii"),
                    "encoding": None,
                }
        return {
            "body": text,
            "body_base64": None,
            "encoding": encoding,
        }
    return {
        "body": None,
        "body_base64": base64.b64encode(raw).decode("ascii") if raw else None,
        "encoding": None,
    }


def _map_exception(exc: BaseException) -> FetchError:
    if isinstance(exc, FetchError):
        return exc

    # httpx wraps many failures; inspect both type and nested cause.
    if isinstance(exc, httpx.TimeoutException):
        return TimeoutError_(str(exc) or "Operation timed out")
    if isinstance(exc, httpx.ConnectTimeout):
        return TimeoutError_(str(exc) or "Connection timed out")
    if isinstance(exc, httpx.ReadTimeout):
        return TimeoutError_(str(exc) or "Read timed out")
    if isinstance(exc, httpx.WriteTimeout):
        return TimeoutError_(str(exc) or "Write timed out")
    if isinstance(exc, httpx.PoolTimeout):
        return TimeoutError_(str(exc) or "Pool timed out")

    if isinstance(exc, httpx.ProxyError):
        return ConnectError_(str(exc) or "Proxy error")

    msg = str(exc) or exc.__class__.__name__
    lower = msg.lower()

    if isinstance(exc, (httpx.ConnectError, httpcore.ConnectError)):
        if "name or service not known" in lower or "nodename nor servname" in lower:
            return DnsError(msg)
        if "temporary failure in name resolution" in lower or "getaddrinfo failed" in lower:
            return DnsError(msg)
        if "certificate" in lower or "ssl" in lower or "tls" in lower:
            return TlsError(msg)
        return ConnectError_(msg)

    if isinstance(exc, (httpx.ConnectError,)):
        return ConnectError_(msg)

    # TLS-related
    if isinstance(exc, httpx.HTTPError) and (
        "certificate" in lower or "ssl" in lower or "tls" in lower
    ):
        return TlsError(msg)

    # Inspect cause chain for more specific errors
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    if cause is not None and cause is not exc:
        mapped = _map_exception(cause)
        if not isinstance(mapped, FetchError) or mapped.error_type != "ERROR":
            # Prefer more specific mapped errors from causes
            if mapped.error_type != "ERROR":
                return mapped

    if isinstance(exc, (httpx.RemoteProtocolError, httpx.LocalProtocolError, httpcore.RemoteProtocolError, httpcore.LocalProtocolError)):
        return ProtocolError_(msg)

    if isinstance(exc, httpx.TooManyRedirects):
        return ProtocolError_(msg)

    if isinstance(exc, httpx.HTTPError):
        return FetchError(msg, error_type="HTTP_ERROR")

    return FetchError(msg, error_type="ERROR")


def _build_transport(
    *,
    verify_tls: bool,
    dns_override: dict[str, str] | None,
) -> httpx.AsyncBaseTransport:
    transport = httpx.AsyncHTTPTransport(verify=verify_tls)
    if dns_override:
        backend = DnsOverrideBackend(dns_override)
        # Replace the connection pool's network backend. httpx does not expose
        # this directly; attaching after construction keeps SNI/Host intact.
        pool = transport._pool  # type: ignore[attr-defined]
        pool._network_backend = backend  # type: ignore[attr-defined]
    return transport


async def fetch_url_raw(
    *,
    url: str,
    method: str | None = None,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout: float | int | None = None,
    follow_redirect: bool = True,
    max_response_bytes: int | None = None,
    dns_override: dict[str, str] | None = None,
    verify_tls: bool = True,
) -> dict[str, Any]:
    """Perform a single stateless HTTP request and return a structured result."""
    try:
        parsed_url = _validate_url(url)
        http_method = _validate_method(method)
        req_headers = _validate_headers(headers)
        timeout_s = _validate_timeout(timeout)
        limit = _validate_max_response_bytes(max_response_bytes)
        overrides = _validate_dns_override(dns_override)

        if not isinstance(follow_redirect, bool):
            raise InvalidParameterError("follow_redirect must be a boolean")
        if not isinstance(verify_tls, bool):
            raise InvalidParameterError("verify_tls must be a boolean")
        if body is not None and not isinstance(body, str):
            raise InvalidParameterError("body must be a string when provided")

        content: bytes | None = None
        if body is not None:
            content = body.encode("utf-8")

        transport = _build_transport(verify_tls=verify_tls, dns_override=overrides)
        timeout_cfg = httpx.Timeout(timeout_s)

        started = time.perf_counter()
        async with httpx.AsyncClient(
            transport=transport,
            timeout=timeout_cfg,
            follow_redirects=follow_redirect,
            max_redirects=DEFAULT_MAX_REDIRECTS,
            http2=False,
            trust_env=False,  # stateless: ignore system proxy env by default
            headers={"User-Agent": "fetch-url-raw/0.1"},
        ) as client:
            request = client.build_request(
                method=http_method,
                url=parsed_url,
                headers=req_headers,
                content=content,
            )
            try:
                response = await client.send(request, stream=True)
            except Exception as exc:  # noqa: BLE001
                raise _map_exception(exc) from exc

            try:
                chunks: list[bytes] = []
                received = 0
                truncated = False
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    if limit == 0:
                        truncated = True
                        break
                    remaining = limit - received
                    if remaining <= 0:
                        truncated = True
                        break
                    if len(chunk) > remaining:
                        chunks.append(chunk[:remaining])
                        received += remaining
                        truncated = True
                        break
                    chunks.append(chunk)
                    received += len(chunk)
                raw = b"".join(chunks)
            except Exception as exc:  # noqa: BLE001
                await response.aclose()
                raise _map_exception(exc) from exc
            else:
                await response.aclose()

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            content_type, charset = _parse_content_type(response.headers.get("content-type"))
            decoded = _decode_body(raw, content_type, charset)

            final_url = str(response.url)
            redirected = final_url.rstrip("/") != str(parsed_url).rstrip("/") or len(response.history) > 0

            # HTTP version string
            http_version = response.http_version or "HTTP/1.1"
            if not http_version.startswith("HTTP"):
                http_version = f"HTTP/{http_version}"

            return {
                "success": True,
                "status": response.status_code,
                "reason": response.reason_phrase or "",
                "http_version": http_version,
                "headers": _headers_to_dict(response.headers),
                "body": decoded["body"],
                "body_base64": decoded["body_base64"],
                "content_type": content_type,
                "encoding": decoded["encoding"],
                "elapsed_ms": elapsed_ms,
                "redirected": redirected,
                "final_url": final_url,
                "truncated": truncated,
                "received_bytes": received,
            }

    except FetchError as err:
        return err.to_dict()
    except Exception as exc:  # noqa: BLE001 - never leak traceback to MCP client
        return _map_exception(exc).to_dict()
