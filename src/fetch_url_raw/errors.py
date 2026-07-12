"""Structured error types for fetch_url_raw."""

from __future__ import annotations

from typing import Any


class FetchError(Exception):
    """Base error that maps cleanly to a structured tool response."""

    error_type: str = "ERROR"

    def __init__(self, message: str, *, error_type: str | None = None) -> None:
        super().__init__(message)
        if error_type is not None:
            self.error_type = error_type
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": False,
            "error": {
                "type": self.error_type,
                "message": self.message,
            },
        }


class InvalidUrlError(FetchError):
    error_type = "INVALID_URL"


class InvalidParameterError(FetchError):
    error_type = "INVALID_PARAMETER"


class DnsError(FetchError):
    error_type = "DNS_ERROR"


class TimeoutError_(FetchError):
    error_type = "TIMEOUT"


class TlsError(FetchError):
    error_type = "TLS_ERROR"


class ConnectError_(FetchError):
    error_type = "CONNECT_ERROR"


class ProtocolError_(FetchError):
    error_type = "PROTOCOL_ERROR"


class ResponseTooLargeError(FetchError):
    error_type = "RESPONSE_TOO_LARGE"


class DestinationBlockedError(FetchError):
    error_type = "DESTINATION_BLOCKED"

