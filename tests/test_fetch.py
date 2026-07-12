"""Unit tests for fetch_url_raw core logic (mock transport)."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from fetch_url_raw.fetch import (
    DEFAULT_MAX_RESPONSE_BYTES,
    TEXT_PREFETCH_BYTES,
    _prefetch_limit,
    _content_length_from_headers,
    _decode_body,
    _is_text_content_type,
    _parse_body_json,
    _resolve_content_length,
    _validate_dns_override,
    _validate_method,
    _validate_url,
    fetch_url_raw,
)
from fetch_url_raw.errors import InvalidParameterError, InvalidUrlError


def test_validate_url_ok():
    u = _validate_url("https://example.com/path?q=1")
    assert u.host == "example.com"
    assert u.scheme == "https"


def test_validate_url_rejects_ftp():
    with pytest.raises(InvalidUrlError):
        _validate_url("ftp://example.com/")


def test_validate_url_rejects_empty():
    with pytest.raises(InvalidUrlError):
        _validate_url("   ")


def test_validate_method_default_and_normalize():
    assert _validate_method(None) == "GET"
    assert _validate_method("post") == "POST"
    assert _validate_method("  DeLeTe ") == "DELETE"


def test_validate_method_rejects_unknown():
    with pytest.raises(InvalidParameterError):
        _validate_method("CONNECT")


def test_validate_dns_override():
    out = _validate_dns_override({"Example.COM.": "1.2.3.4"})
    assert out == {"example.com": "1.2.3.4"}
    with pytest.raises(InvalidParameterError):
        _validate_dns_override({"h": "not-an-ip"})


def test_is_text_content_type():
    assert _is_text_content_type("text/html")
    assert _is_text_content_type("application/json; charset=utf-8")
    assert not _is_text_content_type("image/png")
    assert not _is_text_content_type(None)


def test_decode_body_text_and_binary():
    text = _decode_body(b"hello", "text/plain", "utf-8")
    assert text["body"] == "hello"
    assert text["body_base64"] is None

    binary = _decode_body(b"\xff\x00", "application/octet-stream", None)
    assert binary["body"] is None
    assert binary["body_base64"] == base64.b64encode(b"\xff\x00").decode("ascii")


def test_parse_body_json_only_when_valid():
    assert _parse_body_json(b'{"a": 1}', '{"a": 1}') == {"a": 1}
    assert _parse_body_json(b"[1, 2]", "[1, 2]") == [1, 2]
    assert _parse_body_json(b"not json", "not json") is None
    assert _parse_body_json(b"", "") is None
    # valid JSON without body text (binary path) still parses when UTF-8
    assert _parse_body_json(b'{"x": true}', None) == {"x": True}
    # invalid binary is null
    assert _parse_body_json(b"\xff\x00", None) is None


def test_resolve_content_length():
    assert _resolve_content_length(header_length=500, full_bytes_seen=100, body_complete=False) == 500
    assert _resolve_content_length(header_length=None, full_bytes_seen=42, body_complete=True) == 42
    assert _resolve_content_length(header_length=None, full_bytes_seen=100, body_complete=False) is None


def test_content_length_from_headers():
    assert _content_length_from_headers(httpx.Headers({"content-length": "123"})) == 123
    assert _content_length_from_headers(httpx.Headers({"content-length": "nope"})) is None
    assert _content_length_from_headers(httpx.Headers({})) is None


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/ok":
        return httpx.Response(200, text="hello world", headers={"Content-Type": "text/plain; charset=utf-8"})
    if path == "/json":
        return httpx.Response(200, json={"a": 1}, headers={"Content-Type": "application/json"})
    if path == "/bin":
        return httpx.Response(
            200,
            content=b"\x00\x01\x02\xff",
            headers={"Content-Type": "application/octet-stream"},
        )
    if path == "/big":
        return httpx.Response(
            200,
            content=b"x" * 10_000,
            headers={"Content-Type": "application/octet-stream"},
        )
    if path == "/redirect":
        return httpx.Response(302, headers={"Location": "https://example.com/ok"})
    if path == "/echo":
        body = request.content.decode("utf-8") if request.content else ""
        return httpx.Response(
            200,
            json={
                "method": request.method,
                "headers": dict(request.headers),
                "body": body,
            },
            headers={"Content-Type": "application/json"},
        )
    if path == "/timeout":
        raise httpx.ReadTimeout("read timed out")
    if path == "/dnsfail":
        raise httpx.ConnectError("Name or service not known")
    if path == "/tlsfail":
        raise httpx.ConnectError("SSL: CERTIFICATE_VERIFY_FAILED")
    return httpx.Response(404, text="missing")


@pytest.fixture
def mock_transport(monkeypatch: pytest.MonkeyPatch):
    """Patch AsyncClient so all requests use MockTransport."""
    transport = httpx.MockTransport(_handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        kwargs.pop("verify", None)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    return transport


@pytest.mark.asyncio
async def test_fetch_success_text(mock_transport):
    result = await fetch_url_raw(url="https://example.com/ok")
    assert result["success"] is True
    assert result["status"] == 200
    assert result["body"] == "hello world"
    assert result["body_json"] is None
    assert result["body_base64"] is None
    assert result["truncated"] is False
    assert result["received_bytes"] == len(b"hello world")
    assert result["content_length"] == len(b"hello world")
    assert result["content_type"] == "text/plain"
    assert "elapsed_ms" in result


@pytest.mark.asyncio
async def test_fetch_json_as_text(mock_transport):
    result = await fetch_url_raw(url="https://example.com/json")
    assert result["success"] is True
    assert result["body"] is not None
    assert json.loads(result["body"]) == {"a": 1}
    assert result["body_json"] == {"a": 1}
    assert result["body_base64"] is None
    assert result["content_length"] == result["received_bytes"]


@pytest.mark.asyncio
async def test_fetch_binary_base64(mock_transport):
    result = await fetch_url_raw(url="https://example.com/bin")
    assert result["success"] is True
    assert result["body"] is None
    assert result["body_json"] is None
    assert result["body_base64"] == base64.b64encode(b"\x00\x01\x02\xff").decode("ascii")


@pytest.mark.asyncio
async def test_fetch_truncation(mock_transport):
    result = await fetch_url_raw(url="https://example.com/big", max_response_bytes=100)
    assert result["success"] is True
    assert result["truncated"] is True
    assert result["received_bytes"] == 100
    # MockTransport sets Content-Length from full content, so agents can see real size.
    assert result["content_length"] == 10_000
    raw = base64.b64decode(result["body_base64"])
    assert len(raw) == 100


async def test_fetch_truncation_without_content_length(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        # Chunked-style response without Content-Length.
        return httpx.Response(
            200,
            content=b"y" * 5_000,
            headers={"Content-Type": "application/octet-stream", "Content-Length": ""},
        )

    # Empty content-length should be treated as missing/invalid.
    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        kwargs.pop("verify", None)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    result = await fetch_url_raw(url="https://example.com/chunked", max_response_bytes=50)
    assert result["success"] is True
    assert result["truncated"] is True
    assert result["received_bytes"] == 50
    # Body fits in the 16 MiB prefetch buffer, so full size is known even without Content-Length.
    assert result["content_length"] == 5_000


@pytest.mark.asyncio
async def test_fetch_post_body_and_headers(mock_transport):
    result = await fetch_url_raw(
        url="https://example.com/echo",
        method="post",
        headers={"X-Test": "1", "Content-Type": "application/json"},
        body='{"hello":"world"}',
    )
    assert result["success"] is True
    payload = json.loads(result["body"])
    assert payload["method"] == "POST"
    assert payload["body"] == '{"hello":"world"}'
    # httpx lowercases header names in request.headers mapping
    assert payload["headers"].get("x-test") == "1" or payload["headers"].get("X-Test") == "1"


@pytest.mark.asyncio
async def test_fetch_redirect_follow(mock_transport):
    result = await fetch_url_raw(url="https://example.com/redirect", follow_redirect=True)
    assert result["success"] is True
    assert result["status"] == 200
    assert result["body"] == "hello world"
    assert result["redirected"] is True
    assert result["final_url"].endswith("/ok")


@pytest.mark.asyncio
async def test_fetch_redirect_no_follow(mock_transport):
    result = await fetch_url_raw(url="https://example.com/redirect", follow_redirect=False)
    assert result["success"] is True
    assert result["status"] == 302
    assert "location" in {k.lower() for k in result["headers"]}


@pytest.mark.asyncio
async def test_fetch_timeout_error(mock_transport):
    result = await fetch_url_raw(url="https://example.com/timeout")
    assert result["success"] is False
    assert result["error"]["type"] == "TIMEOUT"


@pytest.mark.asyncio
async def test_fetch_dns_error(mock_transport):
    result = await fetch_url_raw(url="https://example.com/dnsfail")
    assert result["success"] is False
    assert result["error"]["type"] == "DNS_ERROR"


@pytest.mark.asyncio
async def test_fetch_tls_error(mock_transport):
    result = await fetch_url_raw(url="https://example.com/tlsfail")
    assert result["success"] is False
    assert result["error"]["type"] == "TLS_ERROR"


@pytest.mark.asyncio
async def test_fetch_invalid_url_structured():
    result = await fetch_url_raw(url="not-a-url")
    assert result["success"] is False
    assert result["error"]["type"] == "INVALID_URL"


@pytest.mark.asyncio
async def test_fetch_invalid_method_structured():
    result = await fetch_url_raw(url="https://example.com/ok", method="CONNECT")
    assert result["success"] is False
    assert result["error"]["type"] == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_default_max_response_bytes_constant():
    assert DEFAULT_MAX_RESPONSE_BYTES == 1_048_576


# --- Structured exception mapping -------------------------------------------------

from fetch_url_raw.fetch import _map_exception


def _wrap_connect(message: str, cause: BaseException | None = None) -> httpx.ConnectError:
    """Build an httpx.ConnectError chain similar to production (httpx -> httpcore -> cause)."""
    import httpcore

    core: BaseException = httpcore.ConnectError(message)
    if cause is not None:
        # Real stacks often have an intermediate OSError("All connection attempts failed").
        if not isinstance(cause, (httpcore.ConnectError, httpx.ConnectError)):
            outer = OSError("All connection attempts failed")
            outer.__cause__ = cause
            core.__cause__ = outer
        else:
            core.__cause__ = cause
    exc = httpx.ConnectError(message)
    exc.__cause__ = core
    return exc


def test_map_timeout_kinds():
    assert _map_exception(httpx.ConnectTimeout("")).error_type == "TIMEOUT"
    assert "Connection timed out" in _map_exception(httpx.ConnectTimeout("")).message
    assert "reading" in _map_exception(httpx.ReadTimeout("")).message.lower()
    assert "sending" in _map_exception(httpx.WriteTimeout("")).message.lower()


def test_map_dns_error_message():
    err = _map_exception(httpx.ConnectError("Name or service not known"))
    assert err.error_type == "DNS_ERROR"
    assert "DNS resolution failed" in err.message


def test_map_connect_refused_reset_unreachable():
    cases = [
        (ConnectionRefusedError(111, "Connect call failed"), "Connection refused", "CONNECT_ERROR"),
        (ConnectionResetError(104, "Connection reset by peer"), "Connection reset by peer", "CONNECT_ERROR"),
        (OSError(101, "Network is unreachable"), "Network is unreachable", "CONNECT_ERROR"),
        (OSError(113, "No route to host"), "No route to host", "CONNECT_ERROR"),
        (OSError(110, "Connection timed out"), "timed out", "TIMEOUT"),
    ]
    for cause, needle, etype in cases:
        err = _map_exception(_wrap_connect("All connection attempts failed", cause))
        assert err.error_type == etype, (cause, err.error_type, err.message)
        assert needle.lower() in err.message.lower(), err.message


def test_map_tls_certificate_kinds():
    import ssl

    def tls_verify(message: str, verify_message: str, code: int = 1) -> httpx.ConnectError:
        cause = ssl.SSLCertVerificationError(1, message)
        # Attributes present on CPython OpenSSL errors in real failures.
        try:
            cause.verify_message = verify_message  # type: ignore[attr-defined]
            cause.verify_code = code  # type: ignore[attr-defined]
            cause.reason = "CERTIFICATE_VERIFY_FAILED"  # type: ignore[attr-defined]
            cause.library = "SSL"  # type: ignore[attr-defined]
        except Exception:
            pass
        # Even if attrs cannot be set, message string alone should classify.
        return _wrap_connect(message, cause)

    cases = [
        (
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired (_ssl.c:1081)",
            "certificate has expired",
            "expired",
        ),
        (
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate (_ssl.c:1081)",
            "self-signed certificate",
            "self-signed",
        ),
        (
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate in certificate chain (_ssl.c:1081)",
            "self-signed certificate in certificate chain",
            "untrusted",
        ),
        (
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Hostname mismatch, certificate is not valid for 'wrong.host.badssl.com'. (_ssl.c:1081)",
            "Hostname mismatch, certificate is not valid for 'wrong.host.badssl.com'.",
            "hostname mismatch",
        ),
        (
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1081)",
            "unable to get local issuer certificate",
            "incomplete or untrusted",
        ),
    ]
    for message, verify_message, needle in cases:
        err = _map_exception(tls_verify(message, verify_message))
        assert err.error_type == "TLS_ERROR", err.message
        assert needle.lower() in err.message.lower(), (needle, err.message)


def test_map_tls_protocol_and_malformed():
    import ssl
    import httpcore

    def tls_ssl(message: str, reason: str) -> httpx.ConnectError:
        cause = ssl.SSLError(1, message)
        try:
            cause.reason = reason  # type: ignore[attr-defined]
            cause.library = "SSL"  # type: ignore[attr-defined]
        except Exception:
            pass
        core = httpcore.ConnectError(message)
        core.__cause__ = cause
        exc = httpx.ConnectError(message)
        exc.__cause__ = core
        return exc

    expired_proto = _map_exception(
        tls_ssl("[SSL: UNSUPPORTED_PROTOCOL] unsupported protocol (_ssl.c:1081)", "UNSUPPORTED_PROTOCOL")
    )
    assert expired_proto.error_type == "TLS_ERROR"
    assert "protocol" in expired_proto.message.lower() or "outdated" in expired_proto.message.lower()

    malformed = _map_exception(
        tls_ssl("[SSL: RECORD_LAYER_FAILURE] record layer failure (_ssl.c:1081)", "RECORD_LAYER_FAILURE")
    )
    assert malformed.error_type == "TLS_ERROR"
    assert "malformed" in malformed.message.lower() or "unexpected" in malformed.message.lower()


def test_map_destination_blocked_prefix():
    err = _map_exception(
        httpx.ConnectError("DESTINATION_BLOCKED: destination IP 10.0.0.1 is private/local (policy)")
    )
    assert err.error_type == "DESTINATION_BLOCKED"
    assert "10.0.0.1" in err.message


from fetch_url_raw.fetch import _normalize_request_body


def test_normalize_request_body_string_unchanged():
    content, headers = _normalize_request_body("plain=text&a=1", {"Content-Type": "application/x-www-form-urlencoded"})
    assert content == b"plain=text&a=1"
    assert headers == {"Content-Type": "application/x-www-form-urlencoded"}


def test_normalize_request_body_json_object_sets_content_type():
    content, headers = _normalize_request_body({"hello": "world", "n": 1}, None)
    assert json.loads(content.decode("utf-8")) == {"hello": "world", "n": 1}
    assert headers is not None
    assert headers["Content-Type"] == "application/json; charset=utf-8"


def test_normalize_request_body_json_preserves_existing_content_type():
    content, headers = _normalize_request_body(
        {"a": True},
        {"Content-Type": "application/vnd.api+json", "X-Trace": "1"},
    )
    assert json.loads(content.decode("utf-8")) == {"a": True}
    assert headers["Content-Type"] == "application/vnd.api+json"
    assert headers["X-Trace"] == "1"


def test_normalize_request_body_json_array_and_scalars():
    content, _ = _normalize_request_body([1, "x", False], None)
    assert json.loads(content.decode("utf-8")) == [1, "x", False]
    content, _ = _normalize_request_body(42, None)
    assert content == b"42"
    content, _ = _normalize_request_body(True, None)
    assert content == b"true"


def test_normalize_request_body_rejects_unsupported():
    with pytest.raises(InvalidParameterError):
        _normalize_request_body(object(), None)  # type: ignore[arg-type]
    with pytest.raises(InvalidParameterError):
        _normalize_request_body(b"bytes-not-allowed", None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_post_json_object_body(mock_transport):
    result = await fetch_url_raw(
        url="https://example.com/echo",
        method="POST",
        body={"hello": "world", "n": 2},
    )
    assert result["success"] is True
    payload = json.loads(result["body"])
    assert payload["method"] == "POST"
    assert json.loads(payload["body"]) == {"hello": "world", "n": 2}
    # auto Content-Type should be present on the outbound request
    hdrs = {k.lower(): v for k, v in payload["headers"].items()}
    assert "application/json" in hdrs.get("content-type", "")



def test_prefetch_limit_constant():
    assert TEXT_PREFETCH_BYTES == 16 * 1024 * 1024
    assert _prefetch_limit(64 * 1024) == TEXT_PREFETCH_BYTES
    assert _prefetch_limit(20 * 1024 * 1024) == 20 * 1024 * 1024
    assert _prefetch_limit(0) == 0


@pytest.mark.asyncio
async def test_large_text_prefetch_and_truncate(monkeypatch):
    """JS/text larger than max_response_bytes but under 16 MiB should decode as text.

    LLM sees first max_response_bytes of text plus real content_length.
    """
    full = ("// bundle\n" + ("x" * 1000) + "\n").encode("utf-8") * 20  # ~20KB-ish
    # Make ~200KB of javascript-like text
    full = (b"var a=1;" + b"x" * 1024) * 200  # ~200KB+
    assert len(full) > 64 * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=full,
            headers={
                "Content-Type": "text/javascript; charset=utf-8",
                "Content-Length": str(len(full)),
            },
        )

    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        kwargs.pop("verify", None)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    result = await fetch_url_raw(
        url="https://example.com/app.js",
        max_response_bytes=64 * 1024,
    )
    assert result["success"] is True
    assert result["truncated"] is True
    assert result["content_type"] == "text/javascript"
    assert result["body"] is not None
    assert result["body_base64"] is None
    assert result["received_bytes"] == 64 * 1024
    assert result["content_length"] == len(full)
    assert result["body"].startswith("var a=1;")
    # Returned text encodes to the truncated size
    assert len(result["body"].encode("utf-8")) == 64 * 1024


@pytest.mark.asyncio
async def test_text_without_content_length_full_size_from_prefetch(monkeypatch):
    payload = ("hello world\n" * 1000).encode("utf-8")  # ~12KB

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=payload,
            headers={"Content-Type": "text/plain; charset=utf-8", "Content-Length": ""},
        )

    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        kwargs.pop("verify", None)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    result = await fetch_url_raw(url="https://example.com/note.txt", max_response_bytes=100)
    assert result["success"] is True
    assert result["truncated"] is True
    assert result["body"] is not None
    assert result["content_length"] == len(payload)
    assert result["received_bytes"] == 100
