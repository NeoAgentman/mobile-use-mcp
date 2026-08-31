"""Run the installed-wheel Android smoke flow and write safe device evidence.

This is the operator-facing physical-device harness for #18.  It requires an
exact ADB serial, an already built fixture APK, and the exact wheel digest
produced by CI.  The public stdio flow itself is delegated to
``android_ci_acceptance`` so physical and emulator checks keep the same
ClientSession workflow and process cleanup behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

# ``python scripts/android_compatibility_evidence.py`` sets ``sys.path`` to
# ``scripts/`` rather than the repository root. Add the root for direct CLI
# execution while retaining normal package imports for tests.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.android_ci_acceptance import AcceptanceRunnerError, run_acceptance
from scripts.compatibility_evidence import (
    CompatibilityEvidenceError,
    verify_artifact_manifest,
    verify_digest_file,
    write_evidence,
)

FIXTURE_PACKAGE = "com.neoagentman.mobileusefixture"
_FAILURE_STAGE_RE = re.compile(r"android fixture acceptance failed:\s*([a-z][a-z0-9_.-]*)")
_WORKFLOW_RESULT_RE = re.compile(
    r"android fixture compatibility result: screen_recording="
    r"(?P<screen>supported|unsupported|unknown)\s+"
    r"slow_device=(?P<slow>passed|failed|not_tested)\s+"
    r"slow_delay_seconds=(?P<delay>[0-9]+(?:\.[0-9]+)?)\s+"
    r"usb_disconnect=(?P<usb>passed|failed|not_tested)"
)


def _require_non_empty(value: str, name: str) -> str:
    if not value.strip():
        raise CompatibilityEvidenceError(f"{name} must not be empty")
    return value.strip()


def _one_line(value: str, name: str, *, max_length: int = 512) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        return "unknown"
    if len(normalized) > max_length:
        raise CompatibilityEvidenceError(f"device metadata field {name} is too long")
    return normalized


def _run(
    command: list[str],
    stage: str,
    *,
    timeout_seconds: float = 30,
    deadline: float | None = None,
) -> str:
    """Run one bounded command without exposing its output on failure."""

    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CompatibilityEvidenceError(f"device metadata stage failed: {stage}")
        timeout_seconds = min(timeout_seconds, remaining)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        del error
        raise CompatibilityEvidenceError(f"device metadata stage failed: {stage}") from None
    if completed.returncode != 0:
        raise CompatibilityEvidenceError(f"device metadata stage failed: {stage}")
    return completed.stdout.strip()


def parse_adb_version(output: str) -> tuple[str, str]:
    """Extract the ADB and platform-tools versions from ``adb version`` output."""

    adb_match = re.search(r"^Android Debug Bridge version\s+([^\s]+)\s*$", output, re.MULTILINE)
    platform_match = re.search(r"^Version\s+([^\s]+)\s*$", output, re.MULTILINE)
    if adb_match is None or platform_match is None:
        raise CompatibilityEvidenceError("adb version output is incomplete")
    return adb_match.group(1), platform_match.group(1)


def _serial_digest(serial: str) -> str:
    return hashlib.sha256(serial.encode("utf-8")).hexdigest()


def _adb_command(adb: str, serial: str, *arguments: str) -> list[str]:
    return [adb, "-s", serial, *arguments]


def _adb_shell(adb: str, serial: str, *arguments: str, stage: str) -> str:
    return _run(_adb_command(adb, serial, "shell", *arguments), stage)


def _screen_recording_status(
    adb: str, serial: str, *, deadline: float | None = None
) -> str:
    """Probe the built-in binary without retaining its device path or output."""

    timeout_seconds = 30.0
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "unknown"
        timeout_seconds = min(timeout_seconds, remaining)
    try:
        completed = subprocess.run(
            _adb_command(adb, serial, "shell", "command", "-v", "screenrecord"),
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if completed.returncode == 0:
        return "supported" if completed.stdout.strip() else "unsupported"
    # Android shells conventionally use 127 for command-not-found.  A
    # non-zero result with no command path is therefore a useful unsupported
    # capability result; other failures remain unknown.
    if completed.returncode == 127 or not completed.stdout.strip():
        return "unsupported"
    return "unknown"


def _find_adb(adb_path: str | None) -> str:
    candidate = adb_path or shutil.which("adb")
    if candidate is None:
        raise CompatibilityEvidenceError("adb executable is unavailable")
    resolved = shutil.which(candidate) or candidate
    if not Path(resolved).is_file():
        raise CompatibilityEvidenceError("adb executable is unavailable")
    return resolved


def collect_device_facts(
    serial: str,
    *,
    adb_path: str | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Collect only stable, non-secret facts from the explicitly selected device."""

    serial = _require_non_empty(serial, "serial")
    adb = _find_adb(adb_path)
    adb_version, platform_tools_version = parse_adb_version(
        _run([adb, "version"], "adb_version", deadline=deadline)
    )
    state = _run(
        _adb_command(adb, serial, "get-state"), "device_state", deadline=deadline
    )
    if state != "device":
        raise CompatibilityEvidenceError("selected device is not online")

    def prop(name: str) -> str:
        return _one_line(
            _run(
                _adb_command(adb, serial, "shell", "getprop", name),
                f"property_{name}",
                deadline=deadline,
            ),
            name,
        )

    api_raw = prop("ro.build.version.sdk")
    try:
        api_level = int(api_raw)
    except ValueError as error:
        raise CompatibilityEvidenceError("device API level is unavailable") from error
    if api_level < 1:
        raise CompatibilityEvidenceError("device API level is invalid")

    manufacturer = prop("ro.product.manufacturer")
    model = prop("ro.product.model")
    android_release = prop("ro.build.version.release")
    build = prop("ro.build.display.id")
    rom = f"{manufacturer} Android {android_release} ({build})"
    input_method = _one_line(
        _run(
            _adb_command(
                adb, serial, "shell", "settings", "get", "secure", "default_input_method"
            ),
            "input_method",
            deadline=deadline,
        ),
        "input_method",
    )
    if input_method.casefold() in {"(null)", "null", "none"}:
        input_method = "unknown"

    screen_recording_status = _screen_recording_status(adb, serial, deadline=deadline)

    return {
        "adb": adb,
        "adb_version": adb_version,
        "platform_tools_version": platform_tools_version,
        "device": {
            "api_level": api_level,
            "manufacturer": manufacturer,
            "model": model,
            "rom": _one_line(rom, "rom"),
            "serial_sha256": _serial_digest(serial),
        },
        "input_method": {"id": input_method},
        "screen_recording_status": screen_recording_status,
    }


def collect_host_facts(*, adb_version: str, platform_tools_version: str) -> dict[str, str]:
    """Return host/client metadata without usernames, paths, or environment values."""

    return {
        "client": "python-mcp.ClientSession",
        "os": _one_line(
            f"{platform.system()} {platform.release()} {platform.machine()}",
            "host_os",
            max_length=256,
        ),
        "python": platform.python_version(),
        "adb_version": _one_line(adb_version, "adb_version", max_length=64),
        "platform_tools_version": _one_line(
            platform_tools_version,
            "platform_tools_version",
            max_length=128,
        ),
    }


def build_evidence_record(
    *,
    matrix_entry: str,
    observed_at: datetime,
    result: str,
    host: dict[str, Any],
    device: dict[str, Any],
    input_method: dict[str, Any],
    screen_recording_status: str,
    wheel_name: str,
    wheel_digest: str,
    fixture_apk_name: str,
    fixture_apk_digest: str,
    git_commit: str,
    stage_timeout_seconds: float,
    total_timeout_seconds: float,
    duration_seconds: float,
    usb_disconnect_status: str = "not_tested",
    slow_device_status: str = "not_tested",
    slow_delay_seconds: float | None = None,
    failure_stage: str | None = None,
) -> dict[str, Any]:
    """Build the JSON-safe record emitted by a physical compatibility run."""

    timestamp = observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    classification = (
        "over_deadline" if duration_seconds > total_timeout_seconds else "within_deadline"
    )
    unicode_status = "passed" if result == "passed" else "not_tested"
    slow_capability: dict[str, Any] = {
        "status": slow_device_status,
        "classification": classification,
        "source": "public-delay-observation",
    }
    if slow_delay_seconds is not None:
        slow_capability["observed_delay_seconds"] = round(slow_delay_seconds, 3)
    record: dict[str, Any] = {
        "schema_version": 1,
        "matrix_entry": matrix_entry,
        "observed_at": timestamp,
        "observed_date": timestamp[:10],
        "result": result,
        "host": host,
        "device": device,
        "input_method": {
            "id": input_method.get("id", "unknown"),
            "unicode_status": unicode_status,
        },
        "workflow": {
            "name": "android-fixture-public-stdio",
            "status": result,
            "stage_timeout_seconds": stage_timeout_seconds,
            "total_timeout_seconds": total_timeout_seconds,
            "duration_seconds": round(duration_seconds, 3),
            "cleanup": "unconditional",
            "cancellation": "shielded-public-cleanup-and-process-reap",
        },
        "capabilities": {
            "unicode_input": {"status": unicode_status, "source": "public-workflow"},
            "slow_device_behavior": slow_capability,
            "usb_disconnect_recovery": {
                "status": usb_disconnect_status,
                **(
                    {"source": "public-usb-disconnect-flow"}
                    if usb_disconnect_status == "passed"
                    else {}
                ),
            },
            "screen_recording": {
                "status": screen_recording_status,
                "source": "public-recording-flow",
            },
        },
        "artifacts": {
            "wheel": {"filename": wheel_name, "sha256": wheel_digest},
            "fixture_apk": {"filename": fixture_apk_name, "sha256": fixture_apk_digest},
        },
        "artifact_digest": {
            "wheel_sha256": wheel_digest,
            "fixture_apk_sha256": fixture_apk_digest,
        },
        "artifact_lineage": {
            "git_commit": git_commit,
            "wheel_sha256": wheel_digest,
            "fixture_apk_sha256": fixture_apk_digest,
        },
        "privacy": {
            "serial": "sha256",
            "screenshots": False,
            "ui_content": False,
            "typed_text": False,
            "credentials": False,
        },
    }
    if failure_stage is not None:
        record["failure_stage"] = failure_stage
    return record


def _failure_stage(diagnostics: Path) -> str | None:
    try:
        content = diagnostics.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _FAILURE_STAGE_RE.search(content)
    return match.group(1) if match is not None else None


def _workflow_result(diagnostics: Path) -> tuple[str, str, float, str] | None:
    """Read the harness's safe capability marker from redacted diagnostics."""

    try:
        content = diagnostics.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _WORKFLOW_RESULT_RE.search(content)
    if match is None:
        return None
    return (
        match.group("screen"),
        match.group("slow"),
        float(match.group("delay")),
        match.group("usb"),
    )


def run_physical_evidence(
    *,
    serial: str,
    matrix_entry: str,
    server_command: str,
    wheel: Path,
    wheel_digest_file: Path,
    wheel_manifest: Path,
    fixture_apk: Path,
    fixture_digest_file: Path,
    fixture_manifest: Path,
    evidence: Path,
    stage_timeout_seconds: float = 35,
    total_timeout_seconds: float = 180,
    exercise_usb_disconnect: bool = False,
    adb_path: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run physical acceptance and persist a safe result after artifact/device preflight."""

    serial = _require_non_empty(serial, "serial")
    matrix_entry = _require_non_empty(matrix_entry, "matrix_entry")
    if not Path(server_command).is_absolute():
        raise CompatibilityEvidenceError("server-command must be an absolute installed entry point")
    if stage_timeout_seconds <= 0 or total_timeout_seconds <= 0:
        raise CompatibilityEvidenceError("timeouts must be positive")
    wheel = Path(wheel)
    fixture_apk = Path(fixture_apk)
    if wheel.suffix != ".whl":
        raise CompatibilityEvidenceError("wheel must be a .whl artifact")
    if fixture_apk.suffix != ".apk":
        raise CompatibilityEvidenceError("fixture APK must be an .apk artifact")
    operation_started = time.monotonic()
    started_at = datetime.now(UTC)
    operation_deadline = operation_started + total_timeout_seconds
    wheel_digest = verify_digest_file(wheel, Path(wheel_digest_file))
    fixture_digest = verify_digest_file(fixture_apk, Path(fixture_digest_file))
    wheel_commit = verify_artifact_manifest(
        Path(wheel_manifest),
        artifact=wheel,
        kind="wheel",
        expected_digest=wheel_digest,
    )
    fixture_commit = verify_artifact_manifest(
        Path(fixture_manifest),
        artifact=fixture_apk,
        kind="fixture_apk",
        expected_digest=fixture_digest,
    )
    if wheel_commit != fixture_commit:
        raise CompatibilityEvidenceError("wheel and fixture artifacts come from different commits")
    device_facts = collect_device_facts(
        serial,
        adb_path=adb_path,
        deadline=operation_deadline,
    )
    host_facts = collect_host_facts(
        adb_version=cast(str, device_facts["adb_version"]),
        platform_tools_version=cast(str, device_facts["platform_tools_version"]),
    )
    result_code = 1
    result = "failed"
    failure_stage = None
    workflow_result: tuple[str, str, float, str] | None = None
    acceptance_total_timeout = operation_deadline - time.monotonic()
    if acceptance_total_timeout <= 0:
        raise CompatibilityEvidenceError("compatibility total deadline exceeded")
    with tempfile.TemporaryDirectory(prefix="mobile-use-compatibility-") as temporary:
        temporary_root = Path(temporary)
        artifact_root = temporary_root / "artifacts"
        diagnostics = temporary_root / "acceptance.log"
        try:
            result_code = run_acceptance(
                serial=serial,
                package=FIXTURE_PACKAGE,
                server_command=server_command,
                artifact_root=artifact_root,
                diagnostics_path=diagnostics,
                stage_timeout_seconds=stage_timeout_seconds,
                total_timeout_seconds=acceptance_total_timeout,
                exercise_usb_disconnect=exercise_usb_disconnect,
                adb_path=cast(str, device_facts["adb"]),
            )
            result = "passed" if result_code == 0 else "failed"
            failure_stage = None if result == "passed" else _failure_stage(diagnostics)
            workflow_result = _workflow_result(diagnostics)
            if result == "passed" and workflow_result is None:
                result_code = 1
                result = "failed"
                failure_stage = "workflow_observations"
        except KeyboardInterrupt:
            result_code = 130
            result = "cancelled"
        except (AcceptanceRunnerError, OSError, RuntimeError):
            result_code = 1
            result = "failed"
            failure_stage = _failure_stage(diagnostics)
        duration = time.monotonic() - operation_started

    record = build_evidence_record(
        matrix_entry=matrix_entry,
        observed_at=started_at,
        result=result,
        host=host_facts,
        device=cast(dict[str, Any], device_facts["device"]),
        input_method=cast(dict[str, Any], device_facts["input_method"]),
        wheel_name=wheel.name,
        wheel_digest=wheel_digest,
        fixture_apk_name=fixture_apk.name,
        fixture_apk_digest=fixture_digest,
        git_commit=wheel_commit,
        stage_timeout_seconds=stage_timeout_seconds,
        total_timeout_seconds=total_timeout_seconds,
        duration_seconds=duration,
        screen_recording_status=(
            workflow_result[0]
            if workflow_result is not None
            else "not_tested"
        ),
        usb_disconnect_status=workflow_result[3] if workflow_result is not None else "not_tested",
        slow_device_status=workflow_result[1] if workflow_result is not None else "not_tested",
        slow_delay_seconds=workflow_result[2] if workflow_result is not None else None,
        failure_stage=failure_stage,
    )
    # A serial can only be used as the input to its one-way digest.  This
    # catches accidental additions to the record before they reach disk.
    if serial in str(record):
        raise CompatibilityEvidenceError("evidence attempted to retain the complete serial")
    write_evidence(evidence, record)
    return result_code, record


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="Exact authorized ADB serial.")
    parser.add_argument(
        "--matrix-entry",
        required=True,
        help="Stable lower-case name for this compatibility matrix entry.",
    )
    parser.add_argument(
        "--server-command",
        required=True,
        help="Absolute entry point from the CI-installed wheel environment.",
    )
    parser.add_argument("--wheel", type=Path, required=True, help="Exact wheel downloaded from CI.")
    parser.add_argument(
        "--wheel-digest",
        type=Path,
        required=True,
        help="The one-line SHA-256 file generated by the CI build job.",
    )
    parser.add_argument(
        "--wheel-manifest",
        type=Path,
        required=True,
        help="The CI manifest binding the wheel digest to its source commit.",
    )
    parser.add_argument(
        "--fixture-apk", type=Path, required=True, help="Fixture APK downloaded from CI."
    )
    parser.add_argument(
        "--fixture-digest",
        type=Path,
        required=True,
        help="The SHA-256 file generated alongside the CI fixture APK.",
    )
    parser.add_argument(
        "--fixture-manifest",
        type=Path,
        required=True,
        help="The CI manifest binding the fixture APK digest to its source commit.",
    )
    parser.add_argument(
        "--evidence", type=Path, required=True, help="Destination JSON release-evidence file."
    )
    parser.add_argument("--adb-path", help="Optional explicit adb executable path.")
    parser.add_argument("--stage-timeout-seconds", type=float, default=35)
    parser.add_argument("--total-timeout-seconds", type=float, default=180)
    parser.add_argument(
        "--exercise-usb-disconnect",
        action="store_true",
        help="Pause for an operator unplug/replug and verify public reconnect recovery.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result_code, _record = run_physical_evidence(
            serial=args.serial,
            matrix_entry=args.matrix_entry,
            server_command=args.server_command,
            wheel=args.wheel,
            wheel_digest_file=args.wheel_digest,
            wheel_manifest=args.wheel_manifest,
            fixture_apk=args.fixture_apk,
            fixture_digest_file=args.fixture_digest,
            fixture_manifest=args.fixture_manifest,
            evidence=args.evidence,
            stage_timeout_seconds=args.stage_timeout_seconds,
            total_timeout_seconds=args.total_timeout_seconds,
            exercise_usb_disconnect=args.exercise_usb_disconnect,
            adb_path=args.adb_path,
        )
    except KeyboardInterrupt:
        print("android physical compatibility evidence cancelled")
        return 130
    except CompatibilityEvidenceError as error:
        print(f"android physical compatibility evidence failed: {error}")
        return 1
    if result_code == 0:
        print(f"android physical compatibility evidence passed: {args.matrix_entry}")
    else:
        print(f"android physical compatibility evidence failed: {args.matrix_entry}")
    return result_code


if __name__ == "__main__":
    raise SystemExit(main())
