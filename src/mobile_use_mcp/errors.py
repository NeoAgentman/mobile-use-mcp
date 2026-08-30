"""Stable error types returned by the MCP tools."""

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Machine-readable error codes exposed to MCP clients."""

    ADB_UNAVAILABLE = "ADB_UNAVAILABLE"
    DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"
    DEVICE_UNAUTHORIZED = "DEVICE_UNAUTHORIZED"
    DEVICE_OFFLINE = "DEVICE_OFFLINE"
    DEVICE_DISCONNECTED = "DEVICE_DISCONNECTED"
    MULTIPLE_DEVICES = "MULTIPLE_DEVICES"
    NOT_CONNECTED = "NOT_CONNECTED"
    INVALID_TARGET = "INVALID_TARGET"
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    INVALID_COORDINATES = "INVALID_COORDINATES"
    OPERATION_FAILED = "OPERATION_FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    BUSY = "BUSY"
    UNSUPPORTED = "UNSUPPORTED"
    SNAPSHOT_NOT_FOUND = "SNAPSHOT_NOT_FOUND"


class ErrorCategory(StrEnum):
    """Stable high-level categories used by structured lifecycle failures."""

    DEVICE = "device"
    CONNECTION = "connection"
    SESSION = "session"
    VALIDATION = "validation"
    OPERATION = "operation"


_ERROR_CATEGORIES: dict[ErrorCode, ErrorCategory] = {
    ErrorCode.ADB_UNAVAILABLE: ErrorCategory.DEVICE,
    ErrorCode.DEVICE_NOT_FOUND: ErrorCategory.DEVICE,
    ErrorCode.DEVICE_UNAUTHORIZED: ErrorCategory.DEVICE,
    ErrorCode.DEVICE_OFFLINE: ErrorCategory.DEVICE,
    ErrorCode.DEVICE_DISCONNECTED: ErrorCategory.DEVICE,
    ErrorCode.MULTIPLE_DEVICES: ErrorCategory.CONNECTION,
    ErrorCode.NOT_CONNECTED: ErrorCategory.SESSION,
    ErrorCode.SNAPSHOT_NOT_FOUND: ErrorCategory.SESSION,
    ErrorCode.OPERATION_FAILED: ErrorCategory.OPERATION,
    ErrorCode.TIMEOUT: ErrorCategory.OPERATION,
    ErrorCode.CANCELLED: ErrorCategory.OPERATION,
    ErrorCode.BUSY: ErrorCategory.OPERATION,
    ErrorCode.INVALID_TARGET: ErrorCategory.VALIDATION,
    ErrorCode.ELEMENT_NOT_FOUND: ErrorCategory.VALIDATION,
    ErrorCode.INVALID_COORDINATES: ErrorCategory.VALIDATION,
    ErrorCode.UNSUPPORTED: ErrorCategory.OPERATION,
}

_RETRYABLE_CODES = {
    ErrorCode.ADB_UNAVAILABLE,
    ErrorCode.DEVICE_NOT_FOUND,
    ErrorCode.DEVICE_OFFLINE,
    ErrorCode.DEVICE_DISCONNECTED,
    ErrorCode.TIMEOUT,
    ErrorCode.OPERATION_FAILED,
}


class MobileUseError(Exception):
    """Expected operational failure safe to expose to an MCP client."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        suggestion: str | None = None,
        data: dict[str, Any] | None = None,
        *,
        retryable_override: bool | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion
        self.data = data or {}
        self._retryable_override = retryable_override

    @property
    def category(self) -> ErrorCategory:
        """Return a stable category without exposing exception implementation details."""

        return _ERROR_CATEGORIES.get(self.code, ErrorCategory.OPERATION)

    @property
    def retryable(self) -> bool:
        """Whether retrying may succeed without changing the request."""

        if self._retryable_override is not None:
            return self._retryable_override
        return self.code in _RETRYABLE_CODES
