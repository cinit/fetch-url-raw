"""Core HTTP fetch implementation for fetch_url_raw."""

from __future__ import annotations

import asyncio
import base64
import errno
import json
import re
import socket
import ssl
import time
from email.message import Message
from typing import Any

import httpcore
import httpx

from fetch_url_raw import config as runtime_config
from fetch_url_raw.dns import GuardedNetworkBackend
from fetch_url_raw.errors import (
    ConnectError_,
    DestinationBlockedError,
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
# Prefetch ceiling so large text responses can be fully decoded before
# truncating the LLM-facing payload to max_response_bytes.
TEXT_PREFETCH_BYTES = 16 * 1024 * 1024
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


def _header_has(headers: dict[str, str] | None, name: str) -> bool:
    if not headers:
        return False
    target = name.lower()
    return any(key.lower() == target for key in headers)


def _normalize_request_body(
    body: Any,
    headers: dict[str, str] | None,
) -> tuple[bytes | None, dict[str, str] | None]:
    """Accept raw string bodies or JSON values (LLM-friendly).

    - ``None``: no body
    - ``str``: sent as UTF-8 bytes unchanged (no Content-Type change)
    - ``dict`` / ``list`` / ``int`` / ``float`` / ``bool``: JSON-encoded;
      sets ``Content-Type: application/json; charset=utf-8`` when missing
    """
    if body is None:
        return None, headers

    if isinstance(body, str):
        return body.encode("utf-8"), headers

    if isinstance(body, (bytes, bytearray, memoryview)):
        raise InvalidParameterError(
            "body must be a string (raw payload) or a JSON value (object/array/number/boolean), not bytes"
        )

    # bool is a subclass of int — check before broader numeric handling is fine
    # because json.dumps treats bool correctly either way.
    if isinstance(body, (dict, list, int, float, bool)):
        try:
            text = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise InvalidParameterError(f"body is not JSON-serializable: {exc}") from exc
        content = text.encode("utf-8")
        out_headers = dict(headers) if headers is not None else {}
        if not _header_has(out_headers, "content-type"):
            out_headers["Content-Type"] = "application/json; charset=utf-8"
        return content, out_headers if out_headers else headers

    raise InvalidParameterError(
        "body must be a string (raw payload) or a JSON value (object, array, number, or boolean)"
    )


def _prefetch_limit(output_limit: int) -> int:
    """How many body bytes to buffer before decoding/truncating for the LLM.

    Always read at least TEXT_PREFETCH_BYTES (unless output_limit is 0) so a
    multi-megabyte text/JS response can be decoded as text, then truncated to
    max_response_bytes in the tool result.
    """
    if output_limit <= 0:
        return 0
    return max(output_limit, TEXT_PREFETCH_BYTES)


def _truncate_bytes(raw: bytes, limit: int) -> tuple[bytes, bool]:
    if limit <= 0:
        return b"", bool(raw)
    if len(raw) <= limit:
        return raw, False
    return raw[:limit], True


def _truncate_text(text: str, limit: int, encoding: str) -> tuple[str, int, bool]:
    """Truncate decoded text so the UTF-8/encoding byte length is <= limit."""
    if limit <= 0:
        return "", 0, bool(text)
    try:
        encoded = text.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        encoding = "utf-8"
        encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, len(encoded), False
    # Cut on a valid character boundary for the encoding.
    cut = encoded[:limit]
    try:
        out = cut.decode(encoding)
    except UnicodeDecodeError:
        out = cut.decode(encoding, errors="ignore")
    return out, len(out.encode(encoding)), True


def _decode_body(
    raw: bytes,
    content_type: str | None,
    charset: str | None,
    *,
    allow_partial: bool = False,
) -> dict[str, Any]:
    """Decode response bytes.

    When ``allow_partial`` is True (buffer cut at the prefetch limit), tolerate
    incomplete multi-byte sequences at the end so truncated text can still be
    returned to the LLM.
    """
    if _is_text_content_type(content_type):
        encoding = charset or "utf-8"
        errors = "ignore" if allow_partial else "strict"
        try:
            text = raw.decode(encoding, errors=errors)
        except LookupError:
            try:
                text = raw.decode("utf-8", errors=errors)
                encoding = "utf-8"
            except UnicodeDecodeError:
                return {
                    "body": None,
                    "body_base64": base64.b64encode(raw).decode("ascii"),
                    "encoding": None,
                }
        except UnicodeDecodeError:
            try:
                text = raw.decode("utf-8", errors=errors)
                encoding = "utf-8"
            except UnicodeDecodeError:
                if allow_partial:
                    text = raw.decode("utf-8", errors="ignore")
                    encoding = "utf-8"
                else:
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


def _parse_body_json(raw: bytes, body_text: str | None) -> Any | None:
    """Return parsed JSON if and only if the response body is valid JSON; else None."""
    candidates: list[str] = []
    if body_text is not None:
        candidates.append(body_text)
    else:
        # Binary path: still attempt UTF-8 JSON (e.g. mislabeled application/octet-stream).
        try:
            candidates.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            return None

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        stripped = candidate.strip()
        if not stripped:
            continue
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            continue
    return None


def _content_length_from_headers(headers: httpx.Headers) -> int | None:
    """Parse Content-Length header when present and valid."""
    raw_val = headers.get("content-length")
    if raw_val is None:
        return None
    try:
        # Handle possible comma-joined duplicates by taking the first token.
        token = str(raw_val).split(",")[0].strip()
        length = int(token)
    except (TypeError, ValueError):
        return None
    if length < 0:
        return None
    return length


def _resolve_content_length(
    *,
    header_length: int | None,
    full_bytes_seen: int,
    body_complete: bool,
) -> int | None:
    """Actual response body size for agents, especially when output is truncated.

    Preference:
    1. Content-Length header when valid (authoritative full size)
    2. full_bytes_seen when the body was fully read from the wire
    3. None when the stream was cut at the prefetch buffer and total size is unknown
    """
    if header_length is not None:
        return header_length
    if body_complete:
        return full_bytes_seen
    return None


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """Walk __cause__/__context__ without cycles (httpx nests OSError/SSLError)."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _combined_exception_text(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for item in _exception_chain(exc):
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        parts.append(text)
        for attr in ("verify_message", "strerror", "reason"):
            extra = getattr(item, attr, None)
            if extra is None:
                continue
            extra_s = str(extra).strip()
            if extra_s and extra_s not in seen:
                seen.add(extra_s)
                parts.append(extra_s)
    return " | ".join(parts)


def _first_errno(exc: BaseException) -> int | None:
    for item in _exception_chain(exc):
        err = getattr(item, "errno", None)
        if isinstance(err, int):
            return err
        if isinstance(item, OSError) and isinstance(item.args, tuple) and item.args:
            # Some OSError-like wrappers put errno first.
            if isinstance(item.args[0], int):
                return item.args[0]
    # httpcore may strip __cause__; recover errno embedded in messages.
    text = _combined_exception_text(exc)
    match = re.search(r"\[Errno\s+(\d+)\]", text)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def _strip_ssl_c_suffix(text: str) -> str:
    return re.sub(r"\s*\(_ssl\.c:\d+\)\s*$", "", text).strip()


def _extract_cert_verify_detail(text: str) -> str | None:
    marker = "certificate verify failed:"
    lower = text.lower()
    if marker not in lower:
        return None
    idx = lower.index(marker) + len(marker)
    detail = _strip_ssl_c_suffix(text[idx:]).strip(" .")
    return detail or None


def _ssl_verify_message(exc: BaseException) -> str | None:
    for item in _exception_chain(exc):
        vm = getattr(item, "verify_message", None)
        if vm:
            return str(vm).strip()
        if isinstance(item, ssl.SSLCertVerificationError):
            detail = _extract_cert_verify_detail(str(item))
            if detail:
                return detail
            cleaned = _strip_ssl_c_suffix(str(item)).strip()
            return cleaned or None
    # Message-only wrappers (tests / some transports) without SSLCertVerificationError.
    return _extract_cert_verify_detail(_combined_exception_text(exc))


def _ssl_reason(exc: BaseException) -> str | None:
    for item in _exception_chain(exc):
        if isinstance(item, ssl.SSLError):
            reason = getattr(item, "reason", None)
            if reason:
                return str(reason)
    return None


def _classify_timeout(exc: BaseException) -> TimeoutError_:
    if isinstance(exc, httpx.ConnectTimeout):
        return TimeoutError_("Connection timed out while establishing TCP/TLS")
    if isinstance(exc, httpx.ReadTimeout):
        return TimeoutError_("Timed out while reading the response")
    if isinstance(exc, httpx.WriteTimeout):
        return TimeoutError_("Timed out while sending the request")
    if isinstance(exc, httpx.PoolTimeout):
        return TimeoutError_("Timed out waiting for a connection from the pool")
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return TimeoutError_("Operation timed out")

    text = _combined_exception_text(exc).lower()
    err = _first_errno(exc)
    if err == errno.ETIMEDOUT or "connection timed out" in text or "connect call failed" in text and "timed out" in text:
        return TimeoutError_("Connection timed out while establishing TCP/TLS")
    if "read timed out" in text or "timeout reading" in text:
        return TimeoutError_("Timed out while reading the response")
    if "write timed out" in text:
        return TimeoutError_("Timed out while sending the request")
    if isinstance(exc, httpx.TimeoutException):
        detail = str(exc).strip()
        return TimeoutError_(detail or "Operation timed out")
    return TimeoutError_("Operation timed out")


def _classify_dns(exc: BaseException, text: str) -> DnsError | None:
    lower = text.lower()
    dns_markers = (
        "name or service not known",
        "nodename nor servname",
        "temporary failure in name resolution",
        "getaddrinfo failed",
        "name resolution",
        "dns resolution",
        "no address associated with hostname",
        "nodename nor servname provided",
        "gaierror",
    )
    if any(m in lower for m in dns_markers):
        # Prefer a short stable message; keep hostname detail when present.
        raw = str(exc).strip()
        if "Name or service not known:" in raw:
            host = raw.split("Name or service not known:", 1)[-1].strip()
            if host:
                return DnsError(f"DNS resolution failed for {host}")
        if "Name or service not known" in raw:
            return DnsError("DNS resolution failed: name or service not known")
        return DnsError(f"DNS resolution failed: {raw or 'unknown host'}")
    for item in _exception_chain(exc):
        if isinstance(item, socket.gaierror):
            return DnsError(f"DNS resolution failed: {item}")
    return None


def _classify_tls(exc: BaseException, text: str) -> TlsError | None:
    lower = text.lower()
    reason = (_ssl_reason(exc) or "").upper()
    verify_msg = (_ssl_verify_message(exc) or "").strip()
    verify_lower = verify_msg.lower()

    is_tls = (
        isinstance(exc, ssl.SSLError)
        or any(isinstance(item, ssl.SSLError) for item in _exception_chain(exc))
        or "certificate" in lower
        or "ssl" in lower
        or "tls" in lower
        or reason
    )
    if not is_tls:
        return None

    # Certificate verification failures (trust / hostname / validity).
    if (
        isinstance(exc, ssl.SSLCertVerificationError)
        or any(isinstance(item, ssl.SSLCertVerificationError) for item in _exception_chain(exc))
        or "certificate_verify_failed" in lower
        or "certificate verify failed" in lower
        or reason == "CERTIFICATE_VERIFY_FAILED"
    ):
        # Hostname / CNAME / SAN mismatch (cert may otherwise be valid).
        if (
            "hostname mismatch" in verify_lower
            or "hostname mismatch" in lower
            or "not valid for" in verify_lower
            or "not valid for" in lower
            or "doesn't match" in lower
            or "does not match" in lower
            or "ip address mismatch" in verify_lower
            or "ip address mismatch" in lower
        ):
            detail = (verify_msg or "certificate hostname/IP does not match the requested host").rstrip(".")
            return TlsError(
                f"TLS certificate hostname mismatch: {detail}. "
                "The certificate may be valid but was issued for a different name (wrong CNAME/SAN)."
            )

        if "has expired" in verify_lower or "has expired" in lower or "certificate expired" in lower:
            return TlsError(
                "TLS certificate has expired"
                + (f" ({verify_msg})" if verify_msg and "expired" not in verify_lower else "")
            )

        if "not yet valid" in verify_lower or "not yet valid" in lower:
            return TlsError("TLS certificate is not yet valid")

        if (
            "self-signed certificate in certificate chain" in verify_lower
            or "self-signed certificate in certificate chain" in lower
        ):
            return TlsError(
                "TLS certificate chain is untrusted: self-signed certificate in certificate chain "
                "(untrusted/custom CA; use verify_tls=false only if you trust this host)"
            )

        if "self-signed certificate" in verify_lower or "self-signed certificate" in lower:
            return TlsError(
                "TLS certificate is self-signed and not trusted "
                "(use verify_tls=false only if you trust this host)"
            )

        if (
            "unable to get local issuer certificate" in verify_lower
            or "unable to get local issuer certificate" in lower
            or "unable to get issuer certificate" in verify_lower
            or "unable to get issuer certificate" in lower
        ):
            return TlsError(
                "TLS certificate chain incomplete or untrusted: unable to get local issuer certificate "
                "(missing intermediate or untrusted CA)"
            )

        if "unknown ca" in verify_lower or "unable to verify" in lower:
            return TlsError(
                "TLS certificate is untrusted (unknown CA or verification failed"
                + (f": {verify_msg}" if verify_msg else "")
                + ")"
            )

        if verify_msg:
            return TlsError(f"TLS certificate verification failed: {verify_msg}")
        return TlsError("TLS certificate verification failed")

    # Protocol / version / malformed TLS.
    if reason in {
        "UNSUPPORTED_PROTOCOL",
        "TLSV1_ALERT_PROTOCOL_VERSION",
        "WRONG_VERSION_NUMBER",
        "VERSION_TOO_LOW",
        "VERSION_TOO_HIGH",
    } or any(
        m in lower
        for m in (
            "unsupported protocol",
            "wrong version number",
            "tlsv1 alert protocol version",
            "protocol version",
            "version too low",
            "version too high",
        )
    ):
        return TlsError(
            "TLS protocol version mismatch or unsupported/outdated TLS "
            "(peer may only offer obsolete TLS, or non-TLS data was received on an HTTPS connection)"
        )

    if reason in {
        "RECORD_LAYER_FAILURE",
        "UNEXPECTED_EOF_WHILE_READING",
        "WRONG_SSL_VERSION",
        "BAD_RECORD_MAC",
        "DECODE_ERROR",
        "UNEXPECTED_MESSAGE",
    } or any(
        m in lower
        for m in (
            "record layer failure",
            "unexpected eof",
            "eof occurred in violation of protocol",
            "wrong ssl version",
            "bad record mac",
            "packet length too long",
            "httpsconnectionpool",
        )
    ):
        return TlsError(
            "TLS handshake failed: malformed or unexpected TLS data "
            "(often HTTP on an HTTPS port, a middlebox, or a broken peer handshake)"
        )

    if reason in {"SSLV3_ALERT_HANDSHAKE_FAILURE", "HANDSHAKE_FAILURE", "NO_SHARED_CIPHER"} or any(
        m in lower
        for m in (
            "handshake failure",
            "no shared cipher",
            "sslv3 alert handshake failure",
        )
    ):
        return TlsError(
            "TLS handshake failure (no shared cipher/protocol or peer rejected the handshake)"
        )

    if reason == "CERTIFICATE_EXPIRED" or "alert certificate expired" in lower:
        return TlsError("TLS certificate has expired (alert from peer)")

    # Generic TLS/SSL residual.
    detail = verify_msg or str(exc).strip() or reason or "TLS error"
    detail = _strip_ssl_c_suffix(detail)
    return TlsError(f"TLS error: {detail}")


def _classify_connect(exc: BaseException, text: str) -> FetchError:
    lower = text.lower()
    err = _first_errno(exc)

    if "destination_blocked" in lower or "DESTINATION_BLOCKED" in text:
        clean = text.split("DESTINATION_BLOCKED:", 1)[-1].strip() if "DESTINATION_BLOCKED:" in text else text
        # Prefer the first chain message that mentions destination IP.
        for item in _exception_chain(exc):
            s = str(item)
            if "DESTINATION_BLOCKED:" in s:
                clean = s.split("DESTINATION_BLOCKED:", 1)[-1].strip()
                break
            if "destination" in s.lower() and "blocked" in s.lower():
                clean = s
                break
        return DestinationBlockedError(clean or "Destination IP blocked by network policy")

    dns = _classify_dns(exc, text)
    if dns is not None:
        return dns

    tls = _classify_tls(exc, text)
    if tls is not None:
        return tls

    # OS-level connect failures (also from text: httpcore may strip __cause__).
    if (
        err == errno.ECONNREFUSED
        or isinstance(exc, ConnectionRefusedError)
        or any(isinstance(i, ConnectionRefusedError) for i in _exception_chain(exc))
        or "connection refused" in lower
        or "connectionrefusederror" in lower
    ):
        return ConnectError_("Connection refused (no service listening or port closed)")

    if (
        err == errno.ECONNRESET
        or isinstance(exc, ConnectionResetError)
        or any(isinstance(i, ConnectionResetError) for i in _exception_chain(exc))
        or "connection reset by peer" in lower
        or "connection reset" in lower
        or "connectionreseterror" in lower
    ):
        return ConnectError_("Connection reset by peer (TCP RST during connect or request)")

    if (
        err in {errno.ENETUNREACH, errno.EHOSTUNREACH}
        or "network is unreachable" in lower
        or "no route to host" in lower
        or "host is unreachable" in lower
    ):
        if err == errno.ENETUNREACH or "network is unreachable" in lower:
            return ConnectError_("Network is unreachable (no route to destination network)")
        return ConnectError_("No route to host (destination host unreachable)")

    if err == errno.ECONNABORTED or "connection abort" in lower:
        return ConnectError_("Connection aborted")

    if err == errno.EPIPE or "broken pipe" in lower:
        return ConnectError_("Broken pipe (connection closed while sending)")

    if err == errno.ETIMEDOUT or "connection timed out" in lower:
        return TimeoutError_("Connection timed out while establishing TCP/TLS")

    # httpcore often collapses multi-address failures.
    if "all connection attempts failed" in lower:
        # Look deeper for a more specific nested cause (when still present).
        for item in _exception_chain(exc)[1:]:
            nested = _classify_connect(item, _combined_exception_text(item))
            if nested.error_type in {
                "CONNECT_ERROR",
                "TIMEOUT",
                "DNS_ERROR",
                "TLS_ERROR",
                "DESTINATION_BLOCKED",
            } and not nested.message.lower().startswith("all connection attempts failed"):
                return nested
        return ConnectError_("All connection attempts failed (refused, unreachable, or timed out)")

    if isinstance(exc, httpx.ProxyError) or "proxy" in lower:
        detail = str(exc).strip() or "proxy error"
        return ConnectError_(f"Proxy error: {detail}")

    detail = str(exc).strip() or "connection failed"
    if detail.lower() == "all connection attempts failed":
        return ConnectError_("All connection attempts failed (refused, unreachable, or timed out)")
    return ConnectError_(f"Connection failed: {detail}")


def _map_exception(exc: BaseException) -> FetchError:
    if isinstance(exc, FetchError):
        return exc

    # Timeouts first (httpx subclasses TimeoutException).
    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            TimeoutError,
        ),
    ):
        return _classify_timeout(exc)

    text = _combined_exception_text(exc)

    # Prefer structured TLS exceptions even when wrapped.
    tls = _classify_tls(exc, text)
    if tls is not None and (
        isinstance(exc, ssl.SSLError)
        or any(isinstance(item, ssl.SSLError) for item in _exception_chain(exc))
        or "certificate" in text.lower()
        or "ssl" in text.lower()
        or "tls" in text.lower()
    ):
        # Only accept TLS classification when evidence is strong enough;
        # _classify_tls already requires TLS markers.
        if any(
            isinstance(item, ssl.SSLError) for item in _exception_chain(exc)
        ) or any(
            m in text.lower()
            for m in (
                "certificate",
                "ssl",
                "tls",
                "handshake",
            )
        ):
            return tls

    if isinstance(exc, (httpx.ConnectError, httpcore.ConnectError, httpx.ProxyError, OSError)):
        return _classify_connect(exc, text)

    # Protocol errors.
    if isinstance(
        exc,
        (
            httpx.RemoteProtocolError,
            httpx.LocalProtocolError,
            httpcore.RemoteProtocolError,
            httpcore.LocalProtocolError,
        ),
    ):
        detail = str(exc).strip() or "HTTP protocol error"
        return ProtocolError_(detail)

    if isinstance(exc, httpx.TooManyRedirects):
        detail = str(exc).strip() or "Too many redirects"
        return ProtocolError_(detail)

    # Inspect cause chain for more specific transport errors.
    for cause in _exception_chain(exc)[1:]:
        if isinstance(cause, FetchError):
            return cause
        if isinstance(
            cause,
            (
                httpx.TimeoutException,
                TimeoutError,
                httpx.ConnectError,
                httpcore.ConnectError,
                ssl.SSLError,
                OSError,
                socket.gaierror,
            ),
        ):
            mapped = _map_exception(cause)
            if mapped.error_type != "ERROR":
                return mapped

    # Residual TLS-ish HTTP errors (message-based).
    lower = text.lower()
    if isinstance(exc, httpx.HTTPError) and (
        "certificate" in lower or "ssl" in lower or "tls" in lower
    ):
        return _classify_tls(exc, text) or TlsError(str(exc) or "TLS error")

    if isinstance(exc, httpx.HTTPError):
        return FetchError(str(exc).strip() or "HTTP error", error_type="HTTP_ERROR")

    return FetchError(str(exc).strip() or exc.__class__.__name__, error_type="ERROR")


def _build_transport(
    *,
    verify_tls: bool,
    dns_override: dict[str, str] | None,
    allow_private_network: bool | None = None,
) -> httpx.AsyncBaseTransport:
    transport = httpx.AsyncHTTPTransport(verify=verify_tls)
    allow = (
        runtime_config.get_allow_private_network()
        if allow_private_network is None
        else bool(allow_private_network)
    )
    # Always install guarded backend so private destinations are blocked by
    # resolved IP (and dns_override/literal IPs are checked the same way).
    backend = GuardedNetworkBackend(dns_override, allow_private_network=allow)
    pool = transport._pool  # type: ignore[attr-defined]
    pool._network_backend = backend  # type: ignore[attr-defined]
    return transport


async def fetch_url_raw(
    *,
    url: str,
    method: str | None = None,
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: float | int | None = None,
    follow_redirect: bool = True,
    max_response_bytes: int | None = None,
    dns_override: dict[str, str] | None = None,
    verify_tls: bool = True,
    allow_private_network: bool | None = None,
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

        content, req_headers = _normalize_request_body(body, req_headers)

        transport = _build_transport(
            verify_tls=verify_tls,
            dns_override=overrides,
            allow_private_network=allow_private_network,
        )
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

            # Buffer up to max(limit, 16 MiB) so large text can be decoded, then
            # truncate the tool-facing payload to max_response_bytes.
            prefetch = _prefetch_limit(limit)
            try:
                chunks: list[bytes] = []
                buffered = 0
                hit_prefetch_limit = False
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    if prefetch == 0:
                        # max_response_bytes == 0: do not buffer body; mark truncated
                        # if any bytes exist.
                        hit_prefetch_limit = True
                        break
                    remaining = prefetch - buffered
                    if remaining <= 0:
                        hit_prefetch_limit = True
                        break
                    if len(chunk) > remaining:
                        chunks.append(chunk[:remaining])
                        buffered += remaining
                        hit_prefetch_limit = True
                        break
                    chunks.append(chunk)
                    buffered += len(chunk)
                raw_full = b"".join(chunks)
            except Exception as exc:  # noqa: BLE001
                await response.aclose()
                raise _map_exception(exc) from exc
            else:
                await response.aclose()

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            content_type, charset = _parse_content_type(response.headers.get("content-type"))
            body_complete = not hit_prefetch_limit
            header_length = _content_length_from_headers(response.headers)
            content_length = _resolve_content_length(
                header_length=header_length,
                full_bytes_seen=buffered,
                body_complete=body_complete,
            )

            # Decode from the buffered payload (up to 16 MiB). Partial decode is
            # allowed when we hit the prefetch ceiling mid-stream.
            decoded_full = _decode_body(
                raw_full,
                content_type,
                charset,
                allow_partial=hit_prefetch_limit,
            )
            encoding = decoded_full["encoding"]
            body_text: str | None = decoded_full["body"]
            body_b64: str | None = decoded_full["body_base64"]
            truncated = False
            received = buffered

            if body_text is not None:
                body_text, received, out_trunc = _truncate_text(
                    body_text, limit, encoding or "utf-8"
                )
                truncated = out_trunc or hit_prefetch_limit
                body_b64 = None
                raw_for_json = body_text.encode(encoding or "utf-8") if not truncated else b""
            else:
                raw_out, out_trunc = _truncate_bytes(raw_full, limit)
                truncated = out_trunc or hit_prefetch_limit
                received = len(raw_out)
                if raw_out:
                    body_b64 = base64.b64encode(raw_out).decode("ascii")
                else:
                    body_b64 = None
                body_text = None
                raw_for_json = raw_out if not truncated else b""

            # Only expose body_json when the full body is returned (not truncated).
            body_json = (
                _parse_body_json(raw_for_json, body_text) if not truncated else None
            )
            decoded = {
                "body": body_text,
                "body_base64": body_b64,
                "encoding": encoding if body_text is not None else None,
            }

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
                "body_json": body_json,
                "body_base64": decoded["body_base64"],
                "content_type": content_type,
                "encoding": decoded["encoding"],
                "elapsed_ms": elapsed_ms,
                "redirected": redirected,
                "final_url": final_url,
                "truncated": truncated,
                "received_bytes": received,
                "content_length": content_length,
            }

    except FetchError as err:
        return err.to_dict()
    except Exception as exc:  # noqa: BLE001 - never leak traceback to MCP client
        return _map_exception(exc).to_dict()
