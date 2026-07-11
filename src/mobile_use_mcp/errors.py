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
    UNSUPPORTED = "UNSUPPORTED"


class MobileUseError(Exception):
    """Expected operational failure safe to expose to an MCP client."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        suggestion: str | None = None,
        data: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion
        self.data = data or {}
