"""Structured error types for fetch_url_raw."""

from __future__ import annotations

from typing import Any


class FetchError(Exception):
    """Base error that maps cleanly to a structured tool response."""

    error_type: str = "ERROR"

    def __init__(
        self,
        message: str,
        *,
        error_type: str | None = None,
        tls: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if error_type is not None:
            self.error_type = error_type
        self.message = message
        self.tls = tls

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "success": False,
            "error": {
                "type": self.error_type,
                "message": self.message,
            },
        }
        if self.tls is not None:
            payload["tls"] = self.tls
        return payload

    def with_tls(self, tls: dict[str, Any] | None) -> FetchError:
        """Return a copy of this error with TLS metadata attached."""
        if tls is None:
            return self
        return FetchError(self.message, error_type=self.error_type, tls=tls)


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
