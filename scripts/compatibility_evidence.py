"""Validate and render privacy-safe Android compatibility evidence.

The physical-device runner writes one JSON record per explicitly named matrix
entry.  This module deliberately uses only the standard library so release
validation can run before the package is installed.  It is also the single
schema implementation used by the runner, release audit, and matrix renderer.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

SCHEMA_VERSION = 1
DEFAULT_MAX_AGE_DAYS = 180
DEFAULT_FUTURE_SKEW_SECONDS = 300
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
MATRIX_ENTRY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,127}$")
FAILURE_STAGE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")

PASSING_RESULT = "passed"
NON_PASSING_RESULTS = frozenset({"failed", "cancelled"})
CAPABILITY_RESULTS = frozenset(
    {
        "passed",
        "failed",
        "not_tested",
        "supported",
        "unsupported",
        "unknown",
    }
)

_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "matrix_entry",
        "observed_at",
        "observed_date",
        "result",
        "host",
        "device",
        "input_method",
        "workflow",
        "capabilities",
        "artifacts",
        "artifact_digest",
        "artifact_lineage",
        "privacy",
        "failure_stage",
    }
)
_HOST_KEYS = frozenset({"client", "os", "python", "adb_version", "platform_tools_version"})
_DEVICE_KEYS = frozenset({"api_level", "manufacturer", "model", "rom", "serial_sha256"})
_INPUT_METHOD_KEYS = frozenset({"id", "unicode_status"})
_WORKFLOW_KEYS = frozenset(
    {
        "name",
        "status",
        "stage_timeout_seconds",
        "total_timeout_seconds",
        "duration_seconds",
        "cleanup",
        "cancellation",
    }
)
_CAPABILITY_KEYS = frozenset({"status", "source", "classification", "observed_delay_seconds"})
_ARTIFACT_KEYS = frozenset({"filename", "sha256"})
_ARTIFACTS_KEYS = frozenset({"wheel", "fixture_apk"})
_ARTIFACT_DIGEST_KEYS = frozenset({"wheel_sha256", "fixture_apk_sha256"})
_ARTIFACT_LINEAGE_KEYS = frozenset({"git_commit", "wheel_sha256", "fixture_apk_sha256"})
_PRIVACY_KEYS = frozenset({"serial", "screenshots", "ui_content", "typed_text", "credentials"})
_REQUIRED_CAPABILITIES = frozenset(
    {
        "unicode_input",
        "slow_device_behavior",
        "usb_disconnect_recovery",
        "screen_recording",
    }
)
_MATRIX_CONFIG_KEYS = frozenset(
    {"schema_version", "max_age_days", "required_entries", "entries", "notes"}
)
_MATRIX_ENTRY_KEYS = frozenset(
    {
        "api_levels",
        "min_api_level",
        "max_api_level",
        "manufacturers",
        "manufacturer_not_in",
        "required_capabilities",
    }
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|_)(?:serial(?:_number)?|screenshot(?:s)?|ui(?:_content|_text)?|"
    r"typed(?:_text)?|credential(?:s)?|password|token|secret|diagnostic(?:s)?|host_path)(?:$|_)"
)


class CompatibilityEvidenceError(ValueError):
    """Raised when evidence is malformed, unsafe, stale, or incomplete."""


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of one file."""

    digest = sha256()
    try:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise CompatibilityEvidenceError(f"artifact cannot be read: {Path(path).name}") from error
    return digest.hexdigest()


def write_json_file(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically write one generated JSON metadata file."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary_name)
        raise


def verify_digest_file(artifact: Path, digest_file: Path) -> str:
    """Verify a conventional ``sha256  filename`` record and return its digest."""

    try:
        lines = [
            line.strip()
            for line in Path(digest_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError as error:
        raise CompatibilityEvidenceError("artifact digest file cannot be read") from error
    if len(lines) != 1:
        raise CompatibilityEvidenceError("artifact digest file must contain one record")
    fields = lines[0].split(maxsplit=1)
    if len(fields) != 2 or SHA256_RE.fullmatch(fields[0].casefold()) is None:
        raise CompatibilityEvidenceError("artifact digest file has an invalid record")
    recorded_name = fields[1].removeprefix("*")
    if Path(recorded_name).name != Path(artifact).name:
        raise CompatibilityEvidenceError("artifact digest names a different artifact")
    actual = sha256_file(Path(artifact))
    if fields[0].casefold() != actual:
        raise CompatibilityEvidenceError("artifact digest does not match the artifact")
    return actual


def verify_artifact_manifest(
    manifest_path: Path,
    *,
    artifact: Path,
    kind: str,
    expected_digest: str,
) -> str:
    """Verify CI source lineage and artifact identity for one downloaded file."""

    try:
        value = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CompatibilityEvidenceError("artifact manifest cannot be read") from error
    manifest = _mapping(value, "artifact_manifest")
    _assert_allowed_keys(
        manifest,
        "artifact_manifest",
        frozenset({"schema_version", "git_commit", "artifact"}),
    )
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise CompatibilityEvidenceError("unsupported artifact manifest schema version")
    commit = _string(manifest, "git_commit", "artifact_manifest", max_length=40).casefold()
    if GIT_COMMIT_RE.fullmatch(commit) is None:
        raise CompatibilityEvidenceError("artifact manifest git_commit is invalid")
    artifact_record = _mapping(manifest.get("artifact"), "artifact_manifest.artifact")
    _assert_allowed_keys(
        artifact_record,
        "artifact_manifest.artifact",
        frozenset({"kind", "filename", "sha256"}),
    )
    if _string(artifact_record, "kind", "artifact_manifest.artifact", max_length=32) != kind:
        raise CompatibilityEvidenceError("artifact manifest kind does not match the artifact")
    filename = _string(artifact_record, "filename", "artifact_manifest.artifact", max_length=255)
    if Path(filename).name != filename or filename != Path(artifact).name:
        raise CompatibilityEvidenceError("artifact manifest names a different artifact")
    manifest_digest = _string(
        artifact_record, "sha256", "artifact_manifest.artifact", max_length=64
    ).casefold()
    if (
        SHA256_RE.fullmatch(manifest_digest) is None
        or manifest_digest != expected_digest.casefold()
    ):
        raise CompatibilityEvidenceError("artifact manifest digest does not match the artifact")
    return commit


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CompatibilityEvidenceError(f"{context} must be an object")
    return cast(dict[str, Any], value)


def _string(mapping: Mapping[str, Any], key: str, context: str, *, max_length: int = 512) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CompatibilityEvidenceError(f"{context}.{key} must be a non-empty string")
    value = value.strip()
    if len(value) > max_length:
        raise CompatibilityEvidenceError(f"{context}.{key} is too long")
    return value


def _number(mapping: Mapping[str, Any], key: str, context: str) -> int | float:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CompatibilityEvidenceError(f"{context}.{key} must be a number")
    if not math.isfinite(value) or value < 0:
        raise CompatibilityEvidenceError(f"{context}.{key} must not be negative")
    return value


def _status(mapping: Mapping[str, Any], context: str) -> str:
    value = _string(mapping, "status", context, max_length=32)
    if value not in CAPABILITY_RESULTS:
        raise CompatibilityEvidenceError(f"{context}.status is not a known capability result")
    return value


def _parse_timestamp(value: object, context: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CompatibilityEvidenceError(f"{context} must be an RFC3339 timestamp")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        timestamp = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise CompatibilityEvidenceError(f"{context} must be an RFC3339 timestamp") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise CompatibilityEvidenceError(f"{context} must include a timezone")
    return timestamp.astimezone(UTC)


def _assert_allowed_keys(mapping: Mapping[str, Any], context: str, allowed: frozenset[str]) -> None:
    """Reject unknown keys before values can become an evidence data channel."""

    unknown = sorted(str(key) for key in mapping if not isinstance(key, str) or key not in allowed)
    if unknown:
        raise CompatibilityEvidenceError(
            f"{context} contains unknown fields: " + ", ".join(map(str, unknown))
        )


def _walk_forbidden_values(value: object, path: str = "record") -> None:
    """Reject fields that could accidentally turn evidence into a data capture."""

    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = key.casefold().replace("-", "_")
            child_path = f"{path}.{key}"
            # The privacy declaration is the one intentional, non-secret use
            # of the word serial.  A serial hash is accepted separately below.
            allowed_privacy_declaration = path == "record.privacy" and normalized in {
                "serial",
                "screenshots",
                "ui_content",
                "typed_text",
                "credentials",
            }
            if _SENSITIVE_KEY_RE.search(normalized) is not None and not (
                allowed_privacy_declaration or normalized == "serial_sha256"
            ):
                raise CompatibilityEvidenceError(
                    f"privacy-sensitive field is forbidden: {child_path}"
                )
            _walk_forbidden_values(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_forbidden_values(child, f"{path}[{index}]")


def _validate_artifact(value: object, context: str, *, expected_suffix: str) -> dict[str, Any]:
    artifact = _mapping(value, context)
    _assert_allowed_keys(artifact, context, _ARTIFACT_KEYS)
    filename = _string(artifact, "filename", context, max_length=255)
    if Path(filename).name != filename or not filename.endswith(expected_suffix):
        raise CompatibilityEvidenceError(
            f"{context}.filename must be a basename ending in {expected_suffix}"
        )
    digest = _string(artifact, "sha256", context, max_length=64).casefold()
    if SHA256_RE.fullmatch(digest) is None:
        raise CompatibilityEvidenceError(f"{context}.sha256 must be a 64-character hex digest")
    return artifact


def validate_evidence(
    record: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_age_days: int | None = DEFAULT_MAX_AGE_DAYS,
    future_skew_seconds: int = DEFAULT_FUTURE_SKEW_SECONDS,
    require_passed: bool = True,
) -> dict[str, Any]:
    """Validate one evidence record and return it as a JSON-ready mapping.

    ``require_passed=False`` is used while writing a failure/cancellation
    record so the operator gets a safe machine-readable explanation.  Release
    validation keeps the default and therefore accepts only passing evidence.
    """

    raw = _mapping(record, "record")
    _walk_forbidden_values(raw)
    _assert_allowed_keys(raw, "record", _RECORD_KEYS)
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise CompatibilityEvidenceError("unsupported evidence schema version")

    matrix_entry = _string(raw, "matrix_entry", "record", max_length=128)
    if MATRIX_ENTRY_RE.fullmatch(matrix_entry) is None:
        raise CompatibilityEvidenceError("record.matrix_entry must be a lower-case matrix name")
    observed_at = _parse_timestamp(raw.get("observed_at"), "record.observed_at")
    result = _string(raw, "result", "record", max_length=32)
    if result not in {PASSING_RESULT, *NON_PASSING_RESULTS}:
        raise CompatibilityEvidenceError("record.result is invalid")
    if require_passed and result != PASSING_RESULT:
        raise CompatibilityEvidenceError("release evidence must have a passed result")

    current = (now or datetime.now(UTC)).astimezone(UTC)
    if observed_at > current + timedelta(seconds=future_skew_seconds):
        raise CompatibilityEvidenceError("evidence timestamp is in the future")
    if max_age_days is not None:
        if max_age_days <= 0:
            raise CompatibilityEvidenceError("max_age_days must be positive")
        if observed_at < current - timedelta(days=max_age_days):
            raise CompatibilityEvidenceError("evidence is stale")

    host = _mapping(raw.get("host"), "record.host")
    _assert_allowed_keys(host, "record.host", _HOST_KEYS)
    _string(host, "client", "record.host", max_length=128)
    _string(host, "os", "record.host", max_length=256)
    _string(host, "python", "record.host", max_length=64)
    _string(host, "adb_version", "record.host", max_length=64)
    _string(host, "platform_tools_version", "record.host", max_length=128)

    device = _mapping(raw.get("device"), "record.device")
    _assert_allowed_keys(device, "record.device", _DEVICE_KEYS)
    api_level = device.get("api_level")
    if isinstance(api_level, bool) or not isinstance(api_level, int) or api_level < 1:
        raise CompatibilityEvidenceError("record.device.api_level must be a positive integer")
    _string(device, "manufacturer", "record.device", max_length=128)
    _string(device, "model", "record.device", max_length=128)
    _string(device, "rom", "record.device", max_length=256)
    serial_hash = _string(device, "serial_sha256", "record.device", max_length=64).casefold()
    if SHA256_RE.fullmatch(serial_hash) is None:
        raise CompatibilityEvidenceError("record.device.serial_sha256 must be a SHA-256 digest")

    input_method = _mapping(raw.get("input_method"), "record.input_method")
    _assert_allowed_keys(input_method, "record.input_method", _INPUT_METHOD_KEYS)
    _string(input_method, "id", "record.input_method", max_length=256)
    unicode_status = _string(input_method, "unicode_status", "record.input_method", max_length=32)
    if unicode_status not in CAPABILITY_RESULTS:
        raise CompatibilityEvidenceError("record.input_method.unicode_status is invalid")

    workflow = _mapping(raw.get("workflow"), "record.workflow")
    _assert_allowed_keys(workflow, "record.workflow", _WORKFLOW_KEYS)
    _string(workflow, "name", "record.workflow", max_length=128)
    workflow_status = _string(workflow, "status", "record.workflow", max_length=32)
    if workflow_status != result:
        raise CompatibilityEvidenceError("record.workflow.status must match record.result")
    stage_timeout = _number(workflow, "stage_timeout_seconds", "record.workflow")
    total_timeout = _number(workflow, "total_timeout_seconds", "record.workflow")
    duration = _number(workflow, "duration_seconds", "record.workflow")
    if stage_timeout <= 0 or total_timeout <= 0 or duration < 0:
        raise CompatibilityEvidenceError("workflow timeouts and duration must be positive")
    if duration > total_timeout:
        raise CompatibilityEvidenceError("workflow duration exceeds total timeout")
    _string(workflow, "cleanup", "record.workflow", max_length=128)
    _string(workflow, "cancellation", "record.workflow", max_length=256)

    capabilities = _mapping(raw.get("capabilities"), "record.capabilities")
    _assert_allowed_keys(capabilities, "record.capabilities", _REQUIRED_CAPABILITIES)
    missing_capabilities = sorted(_REQUIRED_CAPABILITIES - capabilities.keys())
    if missing_capabilities:
        raise CompatibilityEvidenceError(
            "record.capabilities is missing: " + ", ".join(missing_capabilities)
        )
    for key, value in capabilities.items():
        capability = _mapping(value, f"record.capabilities.{key}")
        _assert_allowed_keys(capability, f"record.capabilities.{key}", _CAPABILITY_KEYS)
        status = _status(capability, f"record.capabilities.{key}")
        if key == "screen_recording":
            source = _string(capability, "source", f"record.capabilities.{key}", max_length=64)
            if source != "public-recording-flow":
                raise CompatibilityEvidenceError(
                    "record.capabilities.screen_recording.source must be public-recording-flow"
                )
        if key == "usb_disconnect_recovery" and status == "passed":
            source = _string(capability, "source", f"record.capabilities.{key}", max_length=96)
            if source != "public-usb-disconnect-flow":
                raise CompatibilityEvidenceError(
                    "record.capabilities.usb_disconnect_recovery.source must be "
                    "public-usb-disconnect-flow"
                )
        if "observed_delay_seconds" in capability:
            observed_delay = _number(
                capability,
                "observed_delay_seconds",
                f"record.capabilities.{key}",
            )
            if observed_delay < 0:
                raise CompatibilityEvidenceError(
                    f"record.capabilities.{key}.observed_delay_seconds must not be negative"
                )
        elif key == "slow_device_behavior" and status == "passed":
            raise CompatibilityEvidenceError(
                "record.capabilities.slow_device_behavior needs observed_delay_seconds"
            )
        if key == "slow_device_behavior" and status == "passed":
            observed_delay = _number(
                capability,
                "observed_delay_seconds",
                f"record.capabilities.{key}",
            )
            if observed_delay < 1.0:
                raise CompatibilityEvidenceError(
                    "record.capabilities.slow_device_behavior delay is not observable"
                )

    artifacts = _mapping(raw.get("artifacts"), "record.artifacts")
    _assert_allowed_keys(artifacts, "record.artifacts", _ARTIFACTS_KEYS)
    wheel_artifact = _validate_artifact(
        artifacts.get("wheel"), "record.artifacts.wheel", expected_suffix=".whl"
    )
    fixture_artifact = _validate_artifact(
        artifacts.get("fixture_apk"), "record.artifacts.fixture_apk", expected_suffix=".apk"
    )
    digest_summary = _mapping(raw.get("artifact_digest"), "record.artifact_digest")
    _assert_allowed_keys(digest_summary, "record.artifact_digest", _ARTIFACT_DIGEST_KEYS)
    artifact_digests: dict[str, str] = {}
    for key in ("wheel_sha256", "fixture_apk_sha256"):
        digest = _string(digest_summary, key, "record.artifact_digest", max_length=64).casefold()
        if SHA256_RE.fullmatch(digest) is None:
            raise CompatibilityEvidenceError(f"record.artifact_digest.{key} is invalid")
        artifact_digests[key] = digest
    if artifact_digests["wheel_sha256"] != wheel_artifact["sha256"].casefold():
        raise CompatibilityEvidenceError("wheel artifact and digest summary disagree")
    if artifact_digests["fixture_apk_sha256"] != fixture_artifact["sha256"].casefold():
        raise CompatibilityEvidenceError("fixture artifact and digest summary disagree")

    lineage = _mapping(raw.get("artifact_lineage"), "record.artifact_lineage")
    _assert_allowed_keys(lineage, "record.artifact_lineage", _ARTIFACT_LINEAGE_KEYS)
    git_commit = _string(lineage, "git_commit", "record.artifact_lineage", max_length=40).casefold()
    if GIT_COMMIT_RE.fullmatch(git_commit) is None:
        raise CompatibilityEvidenceError("record.artifact_lineage.git_commit is invalid")
    for key in ("wheel_sha256", "fixture_apk_sha256"):
        digest = _string(lineage, key, "record.artifact_lineage", max_length=64).casefold()
        if SHA256_RE.fullmatch(digest) is None:
            raise CompatibilityEvidenceError(f"record.artifact_lineage.{key} is invalid")
        if digest != artifact_digests[key]:
            raise CompatibilityEvidenceError(
                f"record.artifact_lineage.{key} disagrees with artifact_digest"
            )

    privacy = _mapping(raw.get("privacy"), "record.privacy")
    _assert_allowed_keys(privacy, "record.privacy", _PRIVACY_KEYS)
    if privacy.get("serial") != "sha256":
        raise CompatibilityEvidenceError("record.privacy.serial must be sha256")
    for key in ("screenshots", "ui_content", "typed_text", "credentials"):
        if privacy.get(key) is not False:
            raise CompatibilityEvidenceError(f"record.privacy.{key} must be false")
    if "observed_date" not in raw or raw["observed_date"] != observed_at.date().isoformat():
        raise CompatibilityEvidenceError("record.observed_date must match observed_at")
    if "failure_stage" in raw:
        failure_stage = _string(raw, "failure_stage", "record", max_length=128)
        if FAILURE_STAGE_RE.fullmatch(failure_stage) is None:
            raise CompatibilityEvidenceError("record.failure_stage is invalid")
    return dict(raw)


def load_evidence(
    path: Path, *, now: datetime | None = None, max_age_days: int | None = DEFAULT_MAX_AGE_DAYS
) -> dict[str, Any]:
    """Read and validate one JSON evidence file."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CompatibilityEvidenceError(
            f"evidence file cannot be read: {Path(path).name}"
        ) from error
    try:
        return validate_evidence(value, now=now, max_age_days=max_age_days)
    except CompatibilityEvidenceError as error:
        raise CompatibilityEvidenceError(f"{Path(path).name}: {error}") from error


def validate_evidence_directory(
    directory: Path,
    *,
    required_entries: Iterable[str] = (),
    now: datetime | None = None,
    max_age_days: int | None = DEFAULT_MAX_AGE_DAYS,
    matrix_constraints: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Validate all JSON records and ensure named entries match their constraints."""

    directory = Path(directory)
    if not directory.is_dir():
        raise CompatibilityEvidenceError("evidence directory does not exist")
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError as error:
        raise CompatibilityEvidenceError("evidence directory cannot be listed") from error
    records = [load_evidence(path, now=now, max_age_days=max_age_days) for path in paths]
    by_entry: dict[str, dict[str, Any]] = {}
    for record in records:
        entry = cast(str, record["matrix_entry"])
        if entry in by_entry:
            raise CompatibilityEvidenceError(f"duplicate matrix entry: {entry}")
        by_entry[entry] = record

    required = tuple(required_entries)
    if len(set(required)) != len(required):
        raise CompatibilityEvidenceError("required matrix entries must be unique")
    missing = [entry for entry in required if entry not in by_entry]
    if missing:
        raise CompatibilityEvidenceError("missing matrix evidence: " + ", ".join(missing))
    if matrix_constraints is not None:
        for record in records:
            entry = cast(str, record["matrix_entry"])
            constraint = matrix_constraints.get(entry)
            if constraint is None:
                raise CompatibilityEvidenceError(f"matrix entry is not configured: {entry}")
            _validate_matrix_record(record, entry, constraint)
    return sorted(records, key=lambda item: cast(str, item["matrix_entry"]))


def _positive_integer(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CompatibilityEvidenceError(f"{context} must be a positive integer")
    return value


def _string_list(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CompatibilityEvidenceError(f"{context} must be a non-empty array")
    items: list[str] = []
    for index, raw_item in enumerate(value):
        if not isinstance(raw_item, str) or not raw_item.strip():
            raise CompatibilityEvidenceError(f"{context}[{index}] must be a non-empty string")
        items.append(raw_item.strip().casefold())
    if len(set(items)) != len(items):
        raise CompatibilityEvidenceError(f"{context} must not contain duplicates")
    return tuple(items)


def _validate_matrix_constraint(name: str, value: object) -> dict[str, Any]:
    constraint = _mapping(value, f"matrix.entries.{name}")
    _assert_allowed_keys(constraint, f"matrix.entries.{name}", _MATRIX_ENTRY_KEYS)
    if not any(key in constraint for key in ("api_levels", "min_api_level", "max_api_level")):
        raise CompatibilityEvidenceError(
            f"matrix.entries.{name} must constrain at least one API level"
        )
    if "api_levels" in constraint:
        raw_levels = constraint["api_levels"]
        if not isinstance(raw_levels, list) or not raw_levels:
            raise CompatibilityEvidenceError(
                f"matrix.entries.{name}.api_levels must be a non-empty array"
            )
        levels = [
            _positive_integer(item, f"matrix.entries.{name}.api_levels") for item in raw_levels
        ]
        if len(set(levels)) != len(levels):
            raise CompatibilityEvidenceError(
                f"matrix.entries.{name}.api_levels must not contain duplicates"
            )
        constraint["api_levels"] = levels
    for key in ("min_api_level", "max_api_level"):
        if key in constraint:
            constraint[key] = _positive_integer(constraint[key], f"matrix.entries.{name}.{key}")
    minimum = constraint.get("min_api_level")
    maximum = constraint.get("max_api_level")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise CompatibilityEvidenceError(f"matrix.entries.{name} has an invalid API range")
    if "manufacturers" in constraint:
        constraint["manufacturers"] = list(
            _string_list(constraint["manufacturers"], f"matrix.entries.{name}.manufacturers")
        )
    if "manufacturer_not_in" in constraint:
        constraint["manufacturer_not_in"] = list(
            _string_list(
                constraint["manufacturer_not_in"],
                f"matrix.entries.{name}.manufacturer_not_in",
            )
        )
    if "required_capabilities" not in constraint:
        raise CompatibilityEvidenceError(
            f"matrix.entries.{name}.required_capabilities must be declared"
        )
    required_capabilities = _mapping(
        constraint["required_capabilities"], f"matrix.entries.{name}.required_capabilities"
    )
    if not required_capabilities:
        raise CompatibilityEvidenceError(
            f"matrix.entries.{name}.required_capabilities must not be empty"
        )
    for capability_name, raw_statuses in required_capabilities.items():
        if capability_name not in _REQUIRED_CAPABILITIES:
            raise CompatibilityEvidenceError(
                f"matrix.entries.{name} names an unknown capability: {capability_name}"
            )
        statuses = _string_list(
            raw_statuses,
            f"matrix.entries.{name}.required_capabilities.{capability_name}",
        )
        if "not_tested" in statuses:
            raise CompatibilityEvidenceError(
                f"matrix.entries.{name} cannot use not_tested as a required status"
            )
        invalid = sorted(set(statuses) - CAPABILITY_RESULTS)
        if invalid:
            raise CompatibilityEvidenceError(
                f"matrix.entries.{name} has unknown capability statuses: {', '.join(invalid)}"
            )
        required_capabilities[capability_name] = list(statuses)
    return constraint


def _validate_matrix_record(
    record: Mapping[str, Any], entry: str, constraint: Mapping[str, Any]
) -> None:
    device = _mapping(record.get("device"), "record.device")
    api_level = cast(int, device["api_level"])
    api_levels = constraint.get("api_levels")
    if isinstance(api_levels, list) and api_level not in api_levels:
        raise CompatibilityEvidenceError(
            f"matrix entry {entry} requires API {api_levels}, record has API {api_level}"
        )
    minimum = constraint.get("min_api_level")
    maximum = constraint.get("max_api_level")
    if isinstance(minimum, int) and api_level < minimum:
        raise CompatibilityEvidenceError(f"matrix entry {entry} requires API >= {minimum}")
    if isinstance(maximum, int) and api_level > maximum:
        raise CompatibilityEvidenceError(f"matrix entry {entry} requires API <= {maximum}")
    manufacturer = cast(str, device["manufacturer"]).casefold()
    manufacturers = constraint.get("manufacturers")
    if isinstance(manufacturers, list) and manufacturer not in manufacturers:
        raise CompatibilityEvidenceError(
            f"matrix entry {entry} requires an OEM in {manufacturers}, record has {manufacturer}"
        )
    excluded = constraint.get("manufacturer_not_in")
    if isinstance(excluded, list) and manufacturer in excluded:
        raise CompatibilityEvidenceError(f"matrix entry {entry} does not allow OEM {manufacturer}")
    capabilities = _mapping(record.get("capabilities"), "record.capabilities")
    required_capabilities = _mapping(
        constraint["required_capabilities"], f"matrix.entries.{entry}.required_capabilities"
    )
    for capability_name, allowed_statuses in required_capabilities.items():
        capability = _mapping(
            capabilities.get(capability_name), f"record.capabilities.{capability_name}"
        )
        status = _status(capability, f"record.capabilities.{capability_name}")
        if status not in allowed_statuses:
            allowed = ", ".join(cast(list[str], allowed_statuses))
            raise CompatibilityEvidenceError(
                f"matrix entry {entry} requires {capability_name} in ({allowed}), got {status}"
            )


def load_matrix_config(
    path: Path,
) -> tuple[tuple[str, ...], int, dict[str, dict[str, Any]]]:
    """Load required entries, freshness, and runtime validation constraints."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CompatibilityEvidenceError(
            f"matrix configuration cannot be read: {Path(path).name}"
        ) from error
    config = _mapping(value, "matrix")
    _assert_allowed_keys(config, "matrix", _MATRIX_CONFIG_KEYS)
    if config.get("schema_version") != SCHEMA_VERSION:
        raise CompatibilityEvidenceError("unsupported matrix configuration schema version")
    raw_entries = config.get("required_entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise CompatibilityEvidenceError("matrix.required_entries must be a non-empty array")
    entries: list[str] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, str) or MATRIX_ENTRY_RE.fullmatch(raw_entry) is None:
            raise CompatibilityEvidenceError("matrix.required_entries contains an invalid name")
        entries.append(raw_entry)
    if len(set(entries)) != len(entries):
        raise CompatibilityEvidenceError("matrix.required_entries must be unique")
    max_age_days = _positive_integer(
        config.get("max_age_days", DEFAULT_MAX_AGE_DAYS), "matrix.max_age_days"
    )
    raw_constraints = _mapping(config.get("entries"), "matrix.entries")
    if set(raw_constraints) != set(entries):
        missing = sorted(set(entries) - set(raw_constraints))
        extra = sorted(set(raw_constraints) - set(entries))
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unknown " + ", ".join(extra))
        raise CompatibilityEvidenceError(
            "matrix.entries must describe each required entry exactly (" + "; ".join(details) + ")"
        )
    constraints = {
        entry: _validate_matrix_constraint(entry, raw_constraints[entry]) for entry in entries
    }
    return tuple(entries), max_age_days, constraints


def load_required_entries(path: Path) -> tuple[str, ...]:
    """Read the named release matrix entries from a small JSON config."""

    entries, _max_age_days, _constraints = load_matrix_config(path)
    return entries


def write_evidence(path: Path, record: Mapping[str, Any]) -> None:
    """Atomically write one safe JSON record without exposing temporary paths."""

    # Structural validation also permits a failure record; release validation
    # calls ``load_evidence`` and therefore still requires ``passed``.
    observed_at = _parse_timestamp(record.get("observed_at"), "record.observed_at")
    validate_evidence(
        record,
        now=observed_at,
        max_age_days=None,
        require_passed=False,
    )
    destination = Path(path)
    write_json_file(destination, record)


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _capability(record: Mapping[str, Any], name: str, field: str = "status") -> str:
    capabilities = _mapping(record.get("capabilities"), "record.capabilities")
    return str(
        _mapping(capabilities.get(name), f"record.capabilities.{name}").get(field, "unknown")
    )


def render_matrix(
    records: Sequence[Mapping[str, Any]],
    *,
    required_entries: Iterable[str] = (),
    matrix_constraints: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Render configured passing evidence into a user-facing matrix with gaps."""

    if records and matrix_constraints is None:
        raise CompatibilityEvidenceError(
            "matrix constraints are required to render evidence records"
        )
    if matrix_constraints is not None:
        for record in records:
            entry = cast(str, record.get("matrix_entry"))
            constraint = matrix_constraints.get(entry)
            if constraint is None:
                raise CompatibilityEvidenceError(f"matrix entry is not configured: {entry}")
            if record.get("result") != PASSING_RESULT:
                raise CompatibilityEvidenceError(f"matrix entry {entry} has a non-passing result")
            _validate_matrix_record(record, entry, constraint)

    entries = {str(entry) for entry in required_entries}
    rows = sorted(records, key=lambda item: str(item.get("matrix_entry", "")))
    lines = [
        "## Android compatibility evidence",
        "",
        "This table is generated from privacy-safe JSON evidence records. A row is a passing, "
        "dated smoke record; unverified combinations remain unclaimed.",
        "",
        "| Matrix entry | Observed (UTC) | Host / Python | Android | Device / ROM | "
        "Unicode IME | Slow device | USB recovery | Recording |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    seen: set[str] = set()
    for record in rows:
        entry = str(record.get("matrix_entry", "unknown"))
        seen.add(entry)
        host = _mapping(record.get("host"), "record.host")
        device = _mapping(record.get("device"), "record.device")
        input_method = _mapping(record.get("input_method"), "record.input_method")
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(entry),
                    _cell(record.get("observed_at", "unknown")),
                    _cell(f"{host.get('os', 'unknown')} / Python {host.get('python', 'unknown')}"),
                    _cell(f"API {device.get('api_level', 'unknown')}"),
                    _cell(
                        f"{device.get('manufacturer', 'unknown')} "
                        f"{device.get('model', 'unknown')} / {device.get('rom', 'unknown')}"
                    ),
                    _cell(input_method.get("unicode_status", _capability(record, "unicode_input"))),
                    _cell(_capability(record, "slow_device_behavior")),
                    _cell(_capability(record, "usb_disconnect_recovery")),
                    _cell(_capability(record, "screen_recording")),
                )
            )
            + " |"
        )
    missing = sorted(entries - seen)
    lines.extend(
        [
            "",
            "### Matrix gaps",
            "",
        ]
    )
    if missing:
        lines.append("The following named entries are unverified and are not support claims:")
        lines.append("")
        lines.extend(f"- `{_cell(entry)}`: unverified" for entry in missing)
    else:
        lines.append("No named matrix entries are missing from the evidence set.")
    return "\n".join(lines) + "\n"


def update_markdown_matrix(
    document: Path,
    records: Sequence[Mapping[str, Any]],
    *,
    required_entries: Iterable[str] = (),
    matrix_constraints: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Replace the generated matrix block while preserving surrounding documentation."""

    begin = "<!-- BEGIN GENERATED ANDROID COMPATIBILITY MATRIX -->"
    end = "<!-- END GENERATED ANDROID COMPATIBILITY MATRIX -->"
    rendered = render_matrix(
        records,
        required_entries=required_entries,
        matrix_constraints=matrix_constraints,
    )
    generated = f"{begin}\n{rendered}{end}"
    path = Path(document)
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as error:
        raise CompatibilityEvidenceError("compatibility document cannot be read") from error
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
    if pattern.search(existing):
        updated = pattern.sub(generated, existing)
    else:
        updated = existing.rstrip() + ("\n\n" if existing.strip() else "") + generated + "\n"
    try:
        path.write_text(updated, encoding="utf-8")
    except OSError as error:
        raise CompatibilityEvidenceError("compatibility document cannot be written") from error
