"""Unit tests for fetch_url_raw core logic (mock transport)."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from fetch_url_raw.fetch import (
    DEFAULT_MAX_RESPONSE_BYTES,
    _decode_body,
    _is_text_content_type,
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
    assert result["body_base64"] is None
    assert result["truncated"] is False
    assert result["received_bytes"] == len(b"hello world")
    assert result["content_type"] == "text/plain"
    assert "elapsed_ms" in result


@pytest.mark.asyncio
async def test_fetch_json_as_text(mock_transport):
    result = await fetch_url_raw(url="https://example.com/json")
    assert result["success"] is True
    assert result["body"] is not None
    assert json.loads(result["body"]) == {"a": 1}
    assert result["body_base64"] is None


@pytest.mark.asyncio
async def test_fetch_binary_base64(mock_transport):
    result = await fetch_url_raw(url="https://example.com/bin")
    assert result["success"] is True
    assert result["body"] is None
    assert result["body_base64"] == base64.b64encode(b"\x00\x01\x02\xff").decode("ascii")


@pytest.mark.asyncio
async def test_fetch_truncation(mock_transport):
    result = await fetch_url_raw(url="https://example.com/big", max_response_bytes=100)
    assert result["success"] is True
    assert result["truncated"] is True
    assert result["received_bytes"] == 100
    raw = base64.b64decode(result["body_base64"])
    assert len(raw) == 100


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
