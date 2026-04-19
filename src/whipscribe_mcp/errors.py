"""Structured error types returned to MCP clients.

Every tool catches exceptions and returns an error object of this shape.
Exceptions must never cross the MCP boundary — they would break the
transport frame and show up in the client as a generic protocol error.
"""

from __future__ import annotations

from typing import Literal, TypedDict

BETA_NOTICE = (
    "Whipscribe MCP is in beta. Endpoints, response shapes, and quotas may "
    "change without notice. Beta credits can be invalidated without notice. "
    "Not suitable for production use cases where transcription failure has "
    "legal, safety, or financial consequences. "
    "Full terms: https://whipscribe.com/terms."
)


ErrorCode = Literal[
    "invalid_input",
    "auth_missing",
    "auth_invalid",
    "quota_exceeded",
    "rate_limited",
    "file_not_found",
    "file_too_large",
    "unsupported_format",
    "upload_failed",
    "url_unreachable",
    "job_not_found",
    "job_failed",
    "job_timeout",
    "transcript_unavailable",
    "network_error",
    "server_error",
    "unknown_error",
]


class ErrorObject(TypedDict):
    code: ErrorCode
    message: str
    retryable: bool


class ToolError(Exception):
    """Raised inside tool handlers; converted to ErrorObject at the boundary."""

    def __init__(self, code: ErrorCode, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
        self.retryable = retryable

    def to_object(self) -> ErrorObject:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


__all__ = [
    "BETA_NOTICE",
    "ErrorCode",
    "ErrorObject",
    "ToolError",
]
