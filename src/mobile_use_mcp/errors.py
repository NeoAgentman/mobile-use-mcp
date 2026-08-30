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
    SNAPSHOT_EXPIRED = "SNAPSHOT_EXPIRED"
    SNAPSHOT_STALE = "SNAPSHOT_STALE"
    SNAPSHOT_TOO_LARGE = "SNAPSHOT_TOO_LARGE"
    ELEMENT_REF_NOT_FOUND = "ELEMENT_REF_NOT_FOUND"
    ELEMENT_REF_EXPIRED = "ELEMENT_REF_EXPIRED"
    ELEMENT_REF_STALE = "ELEMENT_REF_STALE"
    ELEMENT_REMOVED = "ELEMENT_REMOVED"
    ELEMENT_DISABLED = "ELEMENT_DISABLED"
    TARGET_OFF_SCREEN = "TARGET_OFF_SCREEN"
    SELECTOR_CONFLICT = "SELECTOR_CONFLICT"
    INPUT_FOCUS_FAILED = "INPUT_FOCUS_FAILED"
    INPUT_EXECUTION_FAILED = "INPUT_EXECUTION_FAILED"
    INPUT_EXECUTION_UNCERTAIN = "INPUT_EXECUTION_UNCERTAIN"
    CLEAR_EXECUTION_FAILED = "CLEAR_EXECUTION_FAILED"
    CLEAR_EXECUTION_UNCERTAIN = "CLEAR_EXECUTION_UNCERTAIN"
    INPUT_METHOD_RESTORE_FAILED = "INPUT_METHOD_RESTORE_FAILED"
    APP_NOT_FOUND = "APP_NOT_FOUND"
    APP_METADATA_UNAVAILABLE = "APP_METADATA_UNAVAILABLE"
    APP_QUERY_FAILED = "APP_QUERY_FAILED"
    FOREGROUND_UNAVAILABLE = "FOREGROUND_UNAVAILABLE"
    APP_LAUNCH_FAILED = "APP_LAUNCH_FAILED"
    APP_TERMINATION_FAILED = "APP_TERMINATION_FAILED"
    PACKAGE_POLICY_DENIED = "PACKAGE_POLICY_DENIED"
    PACKAGE_POLICY_FOREGROUND_UNAVAILABLE = "PACKAGE_POLICY_FOREGROUND_UNAVAILABLE"
    # Recording transition aliases retain the historical generic operation
    # code for existing clients while exposing descriptive enum names to new
    # callers.
    RECORDING_NOT_ACTIVE = "OPERATION_FAILED"
    RECORDING_ARTIFACT_NOT_FOUND = "RECORDING_ARTIFACT_NOT_FOUND"
    RECORDING_ARTIFACT_EXPIRED = "RECORDING_ARTIFACT_EXPIRED"
    RECORDING_ARTIFACT_UNCLAIMED = "RECORDING_ARTIFACT_UNCLAIMED"
    RECORDING_FAILED = "OPERATION_FAILED"
    RECORDING_NOT_FOUND = "RECORDING_ARTIFACT_NOT_FOUND"
    ARTIFACT_NOT_FOUND = "RECORDING_ARTIFACT_NOT_FOUND"
    ARTIFACT_EXPIRED = "RECORDING_ARTIFACT_EXPIRED"
    # Naming aliases used by embedders that call the boundary a generic
    # policy or package-scope check.
    POLICY_DENIED = "PACKAGE_POLICY_DENIED"
    POLICY_FOREGROUND_UNAVAILABLE = "PACKAGE_POLICY_FOREGROUND_UNAVAILABLE"
    # Naming aliases used by older embedders and integrations.
    APP_DISCOVERY_FAILED = "APP_QUERY_FAILED"
    APP_METADATA_FAILED = "APP_METADATA_UNAVAILABLE"
    FOREGROUND_APP_UNAVAILABLE = "FOREGROUND_UNAVAILABLE"


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
    ErrorCode.SNAPSHOT_EXPIRED: ErrorCategory.SESSION,
    ErrorCode.SNAPSHOT_STALE: ErrorCategory.SESSION,
    ErrorCode.SNAPSHOT_TOO_LARGE: ErrorCategory.SESSION,
    ErrorCode.ELEMENT_REF_NOT_FOUND: ErrorCategory.SESSION,
    ErrorCode.ELEMENT_REF_EXPIRED: ErrorCategory.SESSION,
    ErrorCode.ELEMENT_REF_STALE: ErrorCategory.SESSION,
    ErrorCode.ELEMENT_REMOVED: ErrorCategory.VALIDATION,
    ErrorCode.ELEMENT_DISABLED: ErrorCategory.VALIDATION,
    ErrorCode.TARGET_OFF_SCREEN: ErrorCategory.VALIDATION,
    ErrorCode.SELECTOR_CONFLICT: ErrorCategory.VALIDATION,
    ErrorCode.INPUT_FOCUS_FAILED: ErrorCategory.VALIDATION,
    ErrorCode.INPUT_EXECUTION_FAILED: ErrorCategory.OPERATION,
    ErrorCode.INPUT_EXECUTION_UNCERTAIN: ErrorCategory.OPERATION,
    ErrorCode.CLEAR_EXECUTION_FAILED: ErrorCategory.OPERATION,
    ErrorCode.CLEAR_EXECUTION_UNCERTAIN: ErrorCategory.OPERATION,
    ErrorCode.INPUT_METHOD_RESTORE_FAILED: ErrorCategory.OPERATION,
    ErrorCode.APP_NOT_FOUND: ErrorCategory.VALIDATION,
    ErrorCode.APP_METADATA_UNAVAILABLE: ErrorCategory.DEVICE,
    ErrorCode.APP_QUERY_FAILED: ErrorCategory.DEVICE,
    ErrorCode.FOREGROUND_UNAVAILABLE: ErrorCategory.DEVICE,
    ErrorCode.APP_LAUNCH_FAILED: ErrorCategory.OPERATION,
    ErrorCode.APP_TERMINATION_FAILED: ErrorCategory.OPERATION,
    ErrorCode.PACKAGE_POLICY_DENIED: ErrorCategory.VALIDATION,
    ErrorCode.PACKAGE_POLICY_FOREGROUND_UNAVAILABLE: ErrorCategory.DEVICE,
    ErrorCode.RECORDING_NOT_ACTIVE: ErrorCategory.OPERATION,
    ErrorCode.RECORDING_ARTIFACT_NOT_FOUND: ErrorCategory.OPERATION,
    ErrorCode.RECORDING_ARTIFACT_EXPIRED: ErrorCategory.OPERATION,
    ErrorCode.RECORDING_ARTIFACT_UNCLAIMED: ErrorCategory.OPERATION,
    ErrorCode.RECORDING_FAILED: ErrorCategory.OPERATION,
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
    ErrorCode.APP_METADATA_UNAVAILABLE,
    ErrorCode.APP_QUERY_FAILED,
    ErrorCode.FOREGROUND_UNAVAILABLE,
    ErrorCode.PACKAGE_POLICY_FOREGROUND_UNAVAILABLE,
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
