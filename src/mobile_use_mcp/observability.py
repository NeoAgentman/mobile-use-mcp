"""Local, privacy-safe operation diagnostics.

The MCP transport uses stdout for JSON-RPC frames, so diagnostics must never
share that stream.  This module owns the small amount of logging needed to
reconstruct an operation locally.  It deliberately accepts only operation
metadata; request arguments, Android hierarchy content, screenshots, and
adapter exception text do not cross this boundary.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TextIO, cast

if TYPE_CHECKING:
    from mobile_use_mcp.operations import OperationContext


_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# These names are intentionally broad because this helper is also useful to
# embedders that want to sanity-check a diagnostic payload before forwarding
# it to their own local sink.  The operation logger below does not log
# arbitrary payloads in the first place.
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "text",
        "password",
        "secret",
        "credential",
        "token",
        "authorization",
        "screenshot",
        "image",
        "hierarchy",
        "serial",
        "path",
        "url",
        "environment",
        "exception",
        "traceback",
    }
)
_REDACTED = "[REDACTED]"


def hash_device_serial(serial: str | None) -> str | None:
    """Return a stable SHA-256 identifier without exposing the device serial.

    A stable digest lets an operator correlate failures for one device across
    operations while keeping the complete ADB serial out of diagnostics.  A
    missing serial remains ``None`` so an unavailable device is distinguishable
    from a device whose digest happens to be empty.
    """

    if serial is None or not serial:
        return None
    return hashlib.sha256(serial.encode("utf-8")).hexdigest()


# Descriptive aliases used by integrations that call this a fingerprint.
device_serial_hash = hash_device_serial
fingerprint_device_serial = hash_device_serial


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).casefold().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def redact_value(value: Any, *, key: object | None = None, depth: int = 0) -> Any:
    """Recursively redact values that could contain user or device content.

    This function is intentionally conservative and bounded.  It is exposed
    for tests and embedding applications; normal operation events use the
    fixed metadata allowlist in :class:`PrivacySafeOperationLogger` instead.
    """

    if key is not None and _is_sensitive_key(key):
        return _REDACTED
    if depth > 3:
        return None
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, bytes | bytearray | memoryview):
        return _REDACTED
    if isinstance(value, str):
        # Unknown strings may be UI content, paths, URLs, or exception text.
        # A caller can safely retain a known-safe value by placing it in the
        # operation metadata allowlist instead of passing it here.
        return _REDACTED
    if isinstance(value, Mapping):
        mapping = cast(Mapping[Any, Any], value)
        return {
            str(item_key): redact_value(item_value, key=item_key, depth=depth + 1)
            for item_key, item_value in list(mapping.items())[:50]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(cast(Iterable[Any], value))
        return [redact_value(item, depth=depth + 1) for item in items[:50]]
    if hasattr(value, "model_dump"):
        with_safe_dump = value.model_dump(mode="json")
        return redact_value(with_safe_dump, depth=depth + 1)
    return _REDACTED


@dataclass(frozen=True, slots=True)
class OperationMetadata:
    """Non-content identity captured at operation start.

    ``device_serial`` is retained only as an in-memory bridge between the
    start and finish hooks.  :attr:`device_serial_hash` is the only form that
    is ever serialized to the local diagnostic stream.
    """

    session_id: str | None = None
    generation: int | None = None
    device_serial: str | None = None

    @property
    def device_serial_hash(self) -> str | None:
        return hash_device_serial(self.device_serial)

    def merge(self, fallback: OperationMetadata | None) -> OperationMetadata:
        """Prefer current session identity, falling back to start identity."""

        if fallback is None:
            return self
        return OperationMetadata(
            session_id=self.session_id if self.session_id is not None else fallback.session_id,
            generation=self.generation if self.generation is not None else fallback.generation,
            device_serial=(
                self.device_serial if self.device_serial is not None else fallback.device_serial
            ),
        )


class _StderrJsonHandler(logging.Handler):
    """Write one JSON object per line to the *current* stderr stream.

    Looking up ``sys.stderr`` at emit time matters for stdio tests and for
    embedders that temporarily redirect stderr.  The handler never references
    stdout and never delegates formatting to a potentially unsafe formatter.
    """

    _mobile_use_stderr_handler = True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = getattr(record, "mobile_use_event", None)
            if not isinstance(payload, dict):
                return
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            stream: TextIO = sys.stderr
            stream.write(line + "\n")
            stream.flush()
        except Exception:
            # Diagnostics must never affect MCP request handling.  In
            # particular, do not call ``handleError`` because its traceback
            # would be another uncontrolled stderr payload.
            return


def _local_logger() -> logging.Logger:
    logger = logging.getLogger("mobile_use_mcp.operations")
    logger.propagate = False
    logger.disabled = False
    if not any(
        getattr(handler, "_mobile_use_stderr_handler", False) for handler in logger.handlers
    ):
        handler = _StderrJsonHandler()
        handler.setLevel(logging.DEBUG)
        logger.addHandler(handler)
    return logger


def _safe_level(level: str) -> int:
    normalized = level.strip().upper()
    if normalized not in _LEVELS:
        raise ValueError("log level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
    return _LEVELS[normalized]


class PrivacySafeOperationLogger:
    """Emit bounded operation lifecycle metadata to local stderr only."""

    def __init__(self, level: str = "WARNING") -> None:
        self._logger = _local_logger()
        self.set_level(level)

    @property
    def level(self) -> str:
        numeric = self._logger.level
        return logging.getLevelName(numeric)

    def set_level(self, level: str) -> None:
        """Apply a validated runtime log level to this local sink."""

        self._logger.setLevel(_safe_level(level))

    @staticmethod
    def _metadata_fields(metadata: OperationMetadata | None) -> dict[str, Any]:
        identity = metadata or OperationMetadata()
        return {
            "session_id": identity.session_id,
            "generation": identity.generation,
            "device_serial_hash": identity.device_serial_hash,
        }

    def _emit(self, level: int, payload: dict[str, Any]) -> None:
        # The payload is constructed from known-safe fields only.  Redact one
        # final time before handing it to logging so future additions cannot
        # accidentally turn this stream into a content store.
        safe_payload = {
            key: value
            for key, value in payload.items()
            if key
            in {
                "event",
                "phase",
                "operation",
                "name",
                "operation_id",
                "session_id",
                "generation",
                "device_serial_hash",
                "stage",
                "duration_ms",
                "duration_seconds",
                "result",
                "outcome",
                "error_code",
            }
        }
        # Keep the logging record itself useful to embedders that inspect
        # ``caplog`` or attach another local handler, while the dedicated
        # handler writes the structured object without a logging prefix.
        message = json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":"))
        self._logger.log(level, message, extra={"mobile_use_event": safe_payload})

    def operation_started(
        self,
        operation: OperationContext,
        metadata: OperationMetadata | None = None,
    ) -> None:
        """Record dispatch without any request arguments or content."""

        self._emit(
            logging.DEBUG,
            {
                "event": "operation_started",
                "phase": "start",
                "operation": operation.name,
                "name": operation.name,
                "operation_id": operation.operation_id,
                "stage": operation.stage,
                "result": "started",
                "outcome": "started",
                **self._metadata_fields(metadata),
            },
        )

    def operation_finished(
        self,
        operation: OperationContext,
        metadata: OperationMetadata | None = None,
    ) -> None:
        """Record one terminal operation event with stable error taxonomy."""

        outcome = operation.outcome or (
            "cancelled" if operation.cancelled else "timeout" if operation.timed_out else "failure"
        )
        level = logging.INFO if outcome == "success" else logging.WARNING
        duration_ms = operation.elapsed_ms
        self._emit(
            level,
            {
                "event": "operation_finished",
                "phase": "finish",
                "operation": operation.name,
                "name": operation.name,
                "operation_id": operation.operation_id,
                "stage": operation.stage,
                "duration_ms": duration_ms,
                "duration_seconds": round(duration_ms / 1_000, 6),
                "result": outcome,
                "outcome": outcome,
                "error_code": operation.error_code,
                **self._metadata_fields(metadata),
            },
        )


# Short aliases make the seam discoverable without coupling callers to one
# naming choice.
OperationLogger = PrivacySafeOperationLogger
LocalOperationLogger = PrivacySafeOperationLogger
sanitize_for_log = redact_value
redact_diagnostics = redact_value


__all__ = [
    "LocalOperationLogger",
    "OperationLogger",
    "OperationMetadata",
    "PrivacySafeOperationLogger",
    "device_serial_hash",
    "fingerprint_device_serial",
    "hash_device_serial",
    "redact_diagnostics",
    "redact_value",
    "sanitize_for_log",
]
