"""Validated, immutable runtime configuration for the local Android server.

The MCP server deliberately has one configuration boundary.  Values are read
from environment variables at process start, validated once, and then passed
to the concrete Android adapters.  The module does not use ``BaseSettings`` so
that the runtime dependency remains the small pydantic package already used
by the project.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, cast

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator

DEFAULT_ADB_HOST = "127.0.0.1"
DEFAULT_ADB_PORT = 5037
DEFAULT_OPERATION_TIMEOUT_SECONDS = 30.0
DEFAULT_QUEUE_TIMEOUT_SECONDS = 5.0
DEFAULT_SNAPSHOT_MAX_ELEMENTS = 200
DEFAULT_SNAPSHOT_MAX_TEXT_LENGTH = 500
DEFAULT_PAYLOAD_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 10.0
DEFAULT_SUBPROCESS_MAX_OUTPUT_BYTES = 1 * 1024 * 1024
DEFAULT_SUBPROCESS_TERMINATE_TIMEOUT_SECONDS = 2.0
DEFAULT_ARTIFACT_RETENTION_SECONDS = 24 * 60 * 60
DEFAULT_ARTIFACT_MAX_COUNT = 20
DEFAULT_ARTIFACT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_LOG_LEVEL = "WARNING"


class ConfigurationError(ValueError):
    """Safe startup error whose message never contains configuration values."""

    def __init__(self, fields: tuple[str, ...], reason: str = "invalid value") -> None:
        self.fields = fields
        self.reason = reason
        names = ", ".join(fields) if fields else "runtime settings"
        super().__init__(f"invalid {names}: {reason}")


class RuntimeConfig(BaseModel):
    """Frozen settings shared by discovery, controllers, probes, and artifacts.

    ``adb_path`` is accepted as a constructor alias for compatibility with
    the original ``DeviceRegistry`` API.  ``adb_executable`` is the canonical
    field name used by environment loading and documentation.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    adb_executable: str = Field(
        default_factory=lambda: shutil.which("adb") or "adb", alias="adb_path"
    )
    adb_host: str = Field(
        default=DEFAULT_ADB_HOST,
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("adb_host", "adb_server_host"),
    )
    adb_port: int = Field(
        default=DEFAULT_ADB_PORT,
        ge=1,
        le=65_535,
        validation_alias=AliasChoices("adb_port", "adb_server_port"),
    )

    operation_timeout_seconds: float = Field(
        default=DEFAULT_OPERATION_TIMEOUT_SECONDS, gt=0, le=3_600
    )
    queue_timeout_seconds: float = Field(default=DEFAULT_QUEUE_TIMEOUT_SECONDS, gt=0, le=3_600)

    snapshot_max_elements: int = Field(default=DEFAULT_SNAPSHOT_MAX_ELEMENTS, ge=1, le=10_000)
    snapshot_max_text_length: int = Field(
        default=DEFAULT_SNAPSHOT_MAX_TEXT_LENGTH, ge=1, le=100_000
    )
    payload_max_bytes: int = Field(
        default=DEFAULT_PAYLOAD_MAX_BYTES, ge=1, le=100 * 1024 * 1024
    )

    subprocess_timeout_seconds: float = Field(
        default=DEFAULT_SUBPROCESS_TIMEOUT_SECONDS, gt=0, le=3_600
    )
    subprocess_max_output_bytes: int = Field(
        default=DEFAULT_SUBPROCESS_MAX_OUTPUT_BYTES, ge=1, le=100 * 1024 * 1024
    )
    subprocess_terminate_timeout_seconds: float = Field(
        default=DEFAULT_SUBPROCESS_TERMINATE_TIMEOUT_SECONDS, gt=0, le=120
    )

    artifact_root: Path = Field(
        default_factory=lambda: Path(tempfile.gettempdir()),
    )
    artifact_retention_seconds: int = Field(
        default=DEFAULT_ARTIFACT_RETENTION_SECONDS, ge=0, le=31_536_000
    )
    artifact_max_count: int = Field(default=DEFAULT_ARTIFACT_MAX_COUNT, ge=1, le=10_000)
    artifact_max_bytes: int = Field(
        default=DEFAULT_ARTIFACT_MAX_BYTES, ge=1, le=10 * 1024 * 1024 * 1024
    )

    log_level: str = Field(default=DEFAULT_LOG_LEVEL)

    # The aliases make deployments tolerant of the two names used by older
    # local wrappers while keeping one actual frozen model.
    _environment_aliases: ClassVar[dict[str, tuple[str, ...]]] = {
        "adb_executable": (
            "MOBILE_USE_ADB_EXECUTABLE",
            "MOBILE_USE_ADB_PATH",
            "MOBILE_USE_MCP_ADB_EXECUTABLE",
            "MOBILE_USE_MCP_ADB_PATH",
        ),
        "adb_host": (
            "MOBILE_USE_ADB_HOST",
            "MOBILE_USE_ADB_SERVER_HOST",
            "MOBILE_USE_MCP_ADB_HOST",
            "MOBILE_USE_MCP_ADB_SERVER_HOST",
        ),
        "adb_port": (
            "MOBILE_USE_ADB_PORT",
            "MOBILE_USE_ADB_SERVER_PORT",
            "MOBILE_USE_MCP_ADB_PORT",
            "MOBILE_USE_MCP_ADB_SERVER_PORT",
        ),
        "operation_timeout_seconds": (
            "MOBILE_USE_OPERATION_TIMEOUT_SECONDS",
            "MOBILE_USE_OPERATION_TIMEOUT",
            "MOBILE_USE_MCP_OPERATION_TIMEOUT_SECONDS",
        ),
        "queue_timeout_seconds": (
            "MOBILE_USE_QUEUE_TIMEOUT_SECONDS",
            "MOBILE_USE_QUEUE_TIMEOUT",
            "MOBILE_USE_MCP_QUEUE_TIMEOUT_SECONDS",
        ),
        "snapshot_max_elements": (
            "MOBILE_USE_SNAPSHOT_MAX_ELEMENTS",
            "MOBILE_USE_MCP_SNAPSHOT_MAX_ELEMENTS",
        ),
        "snapshot_max_text_length": (
            "MOBILE_USE_SNAPSHOT_MAX_TEXT_LENGTH",
            "MOBILE_USE_MCP_SNAPSHOT_MAX_TEXT_LENGTH",
        ),
        "payload_max_bytes": (
            "MOBILE_USE_PAYLOAD_MAX_BYTES",
            "MOBILE_USE_MCP_PAYLOAD_MAX_BYTES",
        ),
        "subprocess_timeout_seconds": (
            "MOBILE_USE_SUBPROCESS_TIMEOUT_SECONDS",
            "MOBILE_USE_MCP_SUBPROCESS_TIMEOUT_SECONDS",
        ),
        "subprocess_max_output_bytes": (
            "MOBILE_USE_SUBPROCESS_MAX_OUTPUT_BYTES",
            "MOBILE_USE_MCP_SUBPROCESS_MAX_OUTPUT_BYTES",
        ),
        "subprocess_terminate_timeout_seconds": (
            "MOBILE_USE_SUBPROCESS_TERMINATE_TIMEOUT_SECONDS",
            "MOBILE_USE_MCP_SUBPROCESS_TERMINATE_TIMEOUT_SECONDS",
        ),
        "artifact_root": (
            "MOBILE_USE_ARTIFACT_ROOT",
            "MOBILE_USE_MCP_ARTIFACT_ROOT",
        ),
        "artifact_retention_seconds": (
            "MOBILE_USE_ARTIFACT_RETENTION_SECONDS",
            "MOBILE_USE_ARTIFACT_RETENTION",
            "MOBILE_USE_MCP_ARTIFACT_RETENTION_SECONDS",
        ),
        "artifact_max_count": (
            "MOBILE_USE_ARTIFACT_MAX_COUNT",
            "MOBILE_USE_MCP_ARTIFACT_MAX_COUNT",
        ),
        "artifact_max_bytes": (
            "MOBILE_USE_ARTIFACT_MAX_BYTES",
            "MOBILE_USE_MCP_ARTIFACT_MAX_BYTES",
        ),
        "log_level": ("MOBILE_USE_LOG_LEVEL", "MOBILE_USE_MCP_LOG_LEVEL"),
    }

    @field_validator("adb_executable")
    @classmethod
    def validate_adb_executable(cls, value: str) -> str:
        if not value or "\x00" in value or any(character.isspace() for character in value):
            raise ValueError("must be a single executable path")
        return value

    @field_validator("adb_host")
    @classmethod
    def validate_adb_host(cls, value: str) -> str:
        if not value or "\x00" in value or any(character.isspace() for character in value):
            raise ValueError("must be a host name or address")
        return value

    @field_validator("artifact_root")
    @classmethod
    def validate_artifact_root(cls, value: Path) -> Path:
        # An absolute root makes artifact ownership and retention deterministic
        # regardless of the MCP host's working directory.  Refuse filesystem
        # roots so a typo cannot turn retention cleanup into a broad operation.
        if not value.is_absolute():
            raise ValueError("must be an absolute path")
        if value.parent == value:
            raise ValueError("must not be a filesystem root")
        if "\x00" in str(value):
            raise ValueError("contains an invalid character")
        return value

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")
        return normalized

    @classmethod
    def defaults(cls) -> RuntimeConfig:
        """Return defaults without consulting environment variables."""

        return cls()

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> RuntimeConfig:
        """Load and validate environment settings without leaking raw values."""

        source = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        for field_name, aliases in cls._environment_aliases.items():
            present = next((name for name in aliases if name in source), None)
            if present is not None:
                values[field_name] = source[present]
        try:
            return cls(**values)
        except ValidationError as error:
            # Pydantic's normal error string contains the invalid input value;
            # only expose field names in a startup diagnostic.
            fields: list[str] = []
            errors = cast(list[dict[str, Any]], error.errors())
            for item in errors:
                raw_location: object = item.get("loc", ())
                if isinstance(raw_location, tuple) and raw_location:
                    location = cast(tuple[object, ...], raw_location)
                    first: object = location[0]
                    name = str(first)
                    if name not in fields:
                        fields.append(name)
            if not fields:
                fields = list(values) or ["runtime settings"]
            raise ConfigurationError(tuple(fields)) from None

    # Friendly aliases used by embedders and tests that refer to a generic
    # ``path`` rather than the canonical executable spelling.
    @property
    def adb_path(self) -> str:
        return self.adb_executable

    @property
    def adb_server_host(self) -> str:
        return self.adb_host

    @property
    def adb_server_port(self) -> int:
        return self.adb_port

    @property
    def operation_timeout(self) -> float:
        return self.operation_timeout_seconds

    @property
    def queue_timeout(self) -> float:
        return self.queue_timeout_seconds


def load_runtime_config(environ: Mapping[str, str] | None = None) -> RuntimeConfig:
    """Public configuration loader used by the stdio entry point."""

    return RuntimeConfig.from_environment(environ)


def format_startup_error(error: ConfigurationError) -> str:
    """Return a value-free diagnostic suitable for stderr."""

    fields = ", ".join(error.fields) if error.fields else "runtime settings"
    return (
        "mobile-use-mcp configuration error: invalid "
        f"{fields}; startup stopped without applying a fallback."
    )
