from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.artifact_manifest import write_manifest
from scripts.compatibility_evidence import (
    CompatibilityEvidenceError,
    sha256_file,
    verify_artifact_manifest,
)


def test_manifest_binds_artifact_digest_and_commit(tmp_path: Path) -> None:
    artifact = tmp_path / "mobile_use_mcp-0.4.0-py3-none-any.whl"
    artifact.write_bytes(b"wheel")
    manifest = tmp_path / "wheel.manifest.json"

    write_manifest(artifact, "wheel", "A" * 40, manifest)

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["git_commit"] == "a" * 40
    assert (
        verify_artifact_manifest(
            manifest,
            artifact=artifact,
            kind="wheel",
            expected_digest=sha256_file(artifact),
        )
        == "a" * 40
    )

    artifact.write_bytes(b"tampered")
    with pytest.raises(CompatibilityEvidenceError, match="digest"):
        verify_artifact_manifest(
            manifest,
            artifact=artifact,
            kind="wheel",
            expected_digest=sha256_file(artifact),
        )


def test_manifest_rejects_unknown_fields(tmp_path: Path) -> None:
    artifact = tmp_path / "app-debug.apk"
    artifact.write_bytes(b"apk")
    manifest = tmp_path / "fixture.apk.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "git_commit": "a" * 40,
                "artifact": {
                    "kind": "fixture_apk",
                    "filename": artifact.name,
                    "sha256": sha256_file(artifact),
                    "host_path": "/private/secret",
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CompatibilityEvidenceError, match="unknown fields"):
        verify_artifact_manifest(
            manifest,
            artifact=artifact,
            kind="fixture_apk",
            expected_digest=sha256_file(artifact),
        )


def test_fixture_manifest_uses_the_pinned_ci_python() -> None:
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )

    assert (
        'uv run --no-project --python "$PYTHON_VERSION" python scripts/artifact_manifest.py '
        "--write" in workflow.replace("\\\n", "")
    )
    assert "\n          python scripts/artifact_manifest.py --write" not in workflow


def test_android_job_env_does_not_use_step_only_runner_context() -> None:
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )
    android_job = workflow.split("\n  android-emulator-acceptance:\n", 1)[1]
    job_header = android_job.split("\n    steps:\n", 1)[0]

    assert "${{ runner." not in job_header


def test_android_emulator_uses_a_supported_sdk_channel() -> None:
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )
    android_job = workflow.split("\n  android-emulator-acceptance:\n", 1)[1]

    assert "\n          channel: stable\n" in android_job


def test_android_job_enables_kvm_before_starting_the_emulator() -> None:
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )
    android_job = workflow.split("\n  android-emulator-acceptance:\n", 1)[1]
    kvm_step = "      - name: Enable KVM access on the hosted runner\n"
    emulator_step = "      - name: Start the pinned emulator and run public acceptance\n"

    assert kvm_step in android_job
    assert android_job.index(kvm_step) < android_job.index(emulator_step)
    assert 'KERNEL=="kvm", GROUP="kvm", MODE="0666"' in android_job


def test_android_emulator_script_enters_bash_before_using_pipefail() -> None:
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )
    android_job = workflow.split("\n  android-emulator-acceptance:\n", 1)[1]
    script = android_job.split("\n          script: |\n", 1)[1].split("\n\n", 1)[0]

    assert script.startswith("            bash -euo pipefail <<'BASH'\n")
    assert script.endswith("\n            BASH")
    assert "\n            set -euo pipefail\n" not in script
