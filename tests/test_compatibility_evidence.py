from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

import scripts.android_compatibility_evidence as physical
from scripts.compatibility_evidence import (
    CompatibilityEvidenceError,
    load_matrix_config,
    load_required_entries,
    render_matrix,
    sha256_file,
    validate_evidence,
    validate_evidence_directory,
    write_evidence,
)
from scripts.release_audit import ReleaseAuditError, validate_compatibility_evidence

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _record(
    *,
    entry: str = "physical-huawei-api-29",
    observed_at: datetime = NOW - timedelta(hours=1),
    result: str = "passed",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "matrix_entry": entry,
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "observed_date": observed_at.date().isoformat(),
        "result": result,
        "host": {
            "client": "python-mcp.ClientSession",
            "os": "Darwin 24.6.0 arm64",
            "python": "3.12.11",
            "adb_version": "1.0.41",
            "platform_tools_version": "37.0.1-15733141",
        },
        "device": {
            "api_level": 29,
            "manufacturer": "HUAWEI",
            "model": "EML-AL00",
            "rom": "HarmonyOS 3.0",
            "serial_sha256": "a" * 64,
        },
        "input_method": {
            "id": "com.example.input/.ImeService",
            "unicode_status": "passed",
        },
        "workflow": {
            "name": "android-fixture-public-stdio",
            "status": result,
            "stage_timeout_seconds": 35,
            "total_timeout_seconds": 180,
            "duration_seconds": 12.5,
            "cleanup": "unconditional",
            "cancellation": "shielded-public-cleanup-and-process-reap",
        },
        "capabilities": {
            "unicode_input": {"status": "passed", "source": "public-workflow"},
            "slow_device_behavior": {
                "status": "not_tested",
                "classification": "within_deadline",
                "source": "public-delay-observation",
            },
            "usb_disconnect_recovery": {"status": "not_tested"},
            "screen_recording": {
                "status": "unsupported",
                "source": "public-recording-flow",
            },
        },
        "artifacts": {
            "wheel": {"filename": "mobile_use_mcp-0.4.0-py3-none-any.whl", "sha256": "b" * 64},
            "fixture_apk": {"filename": "app-debug.apk", "sha256": "c" * 64},
        },
        "artifact_digest": {"wheel_sha256": "b" * 64, "fixture_apk_sha256": "c" * 64},
        "artifact_lineage": {
            "git_commit": "d" * 40,
            "wheel_sha256": "b" * 64,
            "fixture_apk_sha256": "c" * 64,
        },
        "privacy": {
            "serial": "sha256",
            "screenshots": False,
            "ui_content": False,
            "typed_text": False,
            "credentials": False,
        },
    }


def test_validate_evidence_accepts_complete_public_record() -> None:
    validated = validate_evidence(_record(), now=NOW)

    assert validated["matrix_entry"] == "physical-huawei-api-29"
    assert validated["device"]["serial_sha256"] == "a" * 64


def test_validate_evidence_rejects_stale_or_failed_records() -> None:
    stale = _record(observed_at=NOW - timedelta(days=31))
    with pytest.raises(CompatibilityEvidenceError, match="stale"):
        validate_evidence(stale, now=NOW, max_age_days=30)

    failed = _record(result="failed")
    with pytest.raises(CompatibilityEvidenceError, match="passed"):
        validate_evidence(failed, now=NOW)


def test_validate_evidence_rejects_complete_serial_and_ui_content() -> None:
    record = _record()
    device = record["device"]
    assert isinstance(device, dict)
    device["serial"] = "SERIAL-UNDER-TEST"
    with pytest.raises(CompatibilityEvidenceError, match="serial"):
        validate_evidence(record, now=NOW)

    record = _record()
    record["ui_content"] = "a captured label"
    with pytest.raises(CompatibilityEvidenceError, match="privacy"):
        validate_evidence(record, now=NOW)


@pytest.mark.parametrize(
    ("location", "field"),
    (
        ("device", "serial_number"),
        ("device", "ui_text"),
        ("host", "host_path"),
        ("capabilities", "screenshot_b64"),
        ("workflow", "diagnostics"),
    ),
)
def test_validate_evidence_rejects_nested_sensitive_payloads(
    location: str, field: str
) -> None:
    record = _record()
    target = cast(dict[str, object], record[location])
    target[field] = "malicious payload"

    with pytest.raises(CompatibilityEvidenceError):
        validate_evidence(record, now=NOW)


def test_validate_evidence_rejects_unknown_fields_even_when_not_sensitive() -> None:
    record = _record()
    record["unapproved_field"] = "must not be persisted"

    with pytest.raises(CompatibilityEvidenceError, match="unknown fields"):
        validate_evidence(record, now=NOW)

    record = _record()
    record[cast(str, 123)] = "non-string key"  # type: ignore[index]
    with pytest.raises(CompatibilityEvidenceError, match="unknown fields"):
        validate_evidence(record, now=NOW)


def test_directory_validation_requires_named_fresh_entries(tmp_path: Path) -> None:
    first = tmp_path / "physical-huawei-api-29.json"
    first.write_text(json.dumps(_record()), encoding="utf-8")

    records = validate_evidence_directory(
        tmp_path,
        required_entries=("physical-huawei-api-29",),
        now=NOW,
        matrix_constraints={
            "physical-huawei-api-29": {
                "api_levels": [29],
                "manufacturers": ["huawei"],
                "required_capabilities": {"unicode_input": ["passed"]},
            }
        },
    )
    assert [item["matrix_entry"] for item in records] == ["physical-huawei-api-29"]

    with pytest.raises(CompatibilityEvidenceError, match="missing"):
        validate_evidence_directory(
            tmp_path,
            required_entries=("physical-huawei-api-29", "android-api-35-emulator"),
            now=NOW,
            matrix_constraints={
                "physical-huawei-api-29": {
                    "api_levels": [29],
                    "manufacturers": ["huawei"],
                    "required_capabilities": {"unicode_input": ["passed"]},
                },
                "android-api-35-emulator": {
                    "api_levels": [35],
                    "manufacturers": ["google"],
                    "required_capabilities": {"screen_recording": ["supported"]},
                },
            },
        )


def test_matrix_configuration_names_the_remaining_release_entries() -> None:
    entries = load_required_entries(PROJECT_ROOT / "compatibility/matrix.json")

    assert entries == (
        "physical-huawei-api-29",
        "physical-aosp-api-26",
        "physical-oem-api-34",
        "physical-recording-supported",
    )

    configured_entries, max_age_days, constraints = load_matrix_config(
        PROJECT_ROOT / "compatibility/matrix.json"
    )
    assert configured_entries == entries
    assert max_age_days == 180
    assert constraints["physical-huawei-api-29"]["api_levels"] == [29]


def test_matrix_configuration_rejects_not_tested_required_capability(tmp_path: Path) -> None:
    config = {
        "schema_version": 1,
        "max_age_days": 180,
        "required_entries": ["physical-test"],
        "entries": {
            "physical-test": {
                "api_levels": [29],
                "required_capabilities": {"slow_device_behavior": ["not_tested"]},
            }
        },
    }
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(CompatibilityEvidenceError, match="not_tested"):
        load_matrix_config(path)


def test_matrix_validation_checks_device_and_required_capabilities(tmp_path: Path) -> None:
    record = _record()
    capabilities = cast(dict[str, object], record["capabilities"])
    capabilities["slow_device_behavior"] = {
        "status": "passed",
        "classification": "within_deadline",
        "source": "public-delay-observation",
        "observed_delay_seconds": 2.0,
    }
    capabilities["usb_disconnect_recovery"] = {
        "status": "passed",
        "source": "public-usb-disconnect-flow",
    }
    path = tmp_path / "physical-huawei-api-29.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    _entries, _age, constraints = load_matrix_config(PROJECT_ROOT / "compatibility/matrix.json")

    validate_evidence_directory(
        tmp_path,
        required_entries=("physical-huawei-api-29",),
        now=NOW,
        matrix_constraints=constraints,
    )

    record["device"]["api_level"] = 30  # type: ignore[index]
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(CompatibilityEvidenceError, match="requires API"):
        validate_evidence_directory(
            tmp_path,
            required_entries=("physical-huawei-api-29",),
            now=NOW,
            matrix_constraints=constraints,
        )


def test_release_audit_rejects_evidence_for_a_different_wheel(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "physical-huawei-api-29.json").write_text(
        json.dumps(_record(observed_at=datetime.now(UTC) - timedelta(minutes=1))),
        encoding="utf-8",
    )
    wheel = tmp_path / "mobile_use_mcp-0.4.0-py3-none-any.whl"
    wheel.write_bytes(b"actual wheel")

    with pytest.raises(ReleaseAuditError, match="different wheel digest"):
        validate_compatibility_evidence(evidence_dir, wheel_path=wheel)


def test_write_evidence_is_machine_readable_and_matrix_is_explicit_about_gaps(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "physical-huawei-api-29.json"
    write_evidence(evidence_path, _record())
    assert json.loads(evidence_path.read_text(encoding="utf-8"))["schema_version"] == 1

    matrix = render_matrix(
        validate_evidence_directory(tmp_path, now=NOW),
        required_entries=("physical-huawei-api-29", "android-api-35-emulator"),
    )
    assert "physical-huawei-api-29" in matrix
    assert "android-api-35-emulator" in matrix
    assert "unverified" in matrix.casefold()
    assert "SERIAL-UNDER-TEST" not in matrix


def test_parse_adb_version_keeps_both_versions() -> None:
    assert physical.parse_adb_version(
        "Android Debug Bridge version 1.0.41\n"
        "Version 37.0.1-15733141\n"
        "Installed as /private/path/adb\n"
    ) == ("1.0.41", "37.0.1-15733141")


def test_physical_runner_uses_explicit_serial_and_writes_only_a_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serial = "SERIAL-UNDER-TEST"
    wheel = tmp_path / "mobile_use_mcp-0.4.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    digest = sha256_file(wheel)
    digest_file = tmp_path / "wheel.sha256"
    digest_file.write_text(f"{digest}  {wheel.name}\n", encoding="utf-8")
    wheel_manifest = tmp_path / "wheel.manifest.json"
    wheel_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "git_commit": "d" * 40,
                "artifact": {"kind": "wheel", "filename": wheel.name, "sha256": digest},
            }
        ),
        encoding="utf-8",
    )
    fixture = tmp_path / "app-debug.apk"
    fixture.write_bytes(b"apk")
    fixture_digest = sha256_file(fixture)
    fixture_digest_file = tmp_path / "fixture.apk.sha256"
    fixture_digest_file.write_text(f"{fixture_digest}  {fixture.name}\n", encoding="utf-8")
    fixture_manifest = tmp_path / "fixture.apk.manifest.json"
    fixture_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "git_commit": "d" * 40,
                "artifact": {
                    "kind": "fixture_apk",
                    "filename": fixture.name,
                    "sha256": fixture_digest,
                },
            }
        ),
        encoding="utf-8",
    )

    def fake_facts(_selected: str, **_kwargs: object) -> dict[str, object]:
        return {
            "adb": "/usr/bin/adb",
            "adb_version": "1.0.41",
            "platform_tools_version": "37.0.1-15733141",
            "device": {
                "api_level": 29,
                "manufacturer": "HUAWEI",
                "model": "EML-AL00",
                "rom": "HarmonyOS 3.0",
                "serial_sha256": "a" * 64,
            },
            "input_method": {"id": "com.example/.ImeService"},
            "screen_recording_status": "unsupported",
        }

    monkeypatch.setattr(physical, "collect_device_facts", fake_facts)
    calls: list[str] = []

    def fake_acceptance(**kwargs: object) -> int:
        calls.append(cast(str, kwargs["serial"]))
        diagnostics = cast(Path, kwargs["diagnostics_path"])
        diagnostics.write_text(
            "android fixture compatibility result: "
            "screen_recording=unsupported slow_device=passed "
            "slow_delay_seconds=2.000 usb_disconnect=not_tested\n",
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(physical, "run_acceptance", fake_acceptance)
    code, record = physical.run_physical_evidence(
        serial=serial,
        matrix_entry="physical-huawei-api-29",
        server_command="/tmp/installed/bin/mobile-use-mcp",
        wheel=wheel,
        wheel_digest_file=digest_file,
        wheel_manifest=wheel_manifest,
        fixture_apk=fixture,
        fixture_digest_file=fixture_digest_file,
        fixture_manifest=fixture_manifest,
        evidence=tmp_path / "evidence.json",
    )

    assert code == 0
    assert calls == [serial]
    encoded = (tmp_path / "evidence.json").read_text(encoding="utf-8")
    assert serial not in encoded
    assert record["device"]["serial_sha256"] == "a" * 64
    assert record["artifact_lineage"]["git_commit"] == "d" * 40
