from __future__ import annotations

import importlib.metadata
from pathlib import Path

import pytest

from mobile_use_mcp import __version__
from scripts.release_audit import (
    ReleaseAuditError,
    expected_release_tag,
    validate_compatibility_evidence,
    validate_release_tag,
    validate_source_provenance,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_version_matches_installed_distribution_metadata() -> None:
    assert __version__ == importlib.metadata.version("mobile-use-mcp")


def test_release_tag_is_derived_from_runtime_version() -> None:
    assert expected_release_tag() == f"v{__version__}"
    validate_release_tag(f"v{__version__}")

    with pytest.raises(ReleaseAuditError, match="does not match"):
        validate_release_tag("v0.0.0")


def test_source_provenance_inventory_is_current() -> None:
    validate_source_provenance(PROJECT_ROOT / "provenance.toml", PROJECT_ROOT)


def test_provenance_python_bytes_are_stable_across_checkouts() -> None:
    attributes = (PROJECT_ROOT / ".gitattributes").read_text(encoding="utf-8")

    assert "*.py text eol=lf" in attributes.splitlines()


def test_source_provenance_detects_source_drift(tmp_path: Path) -> None:
    source = PROJECT_ROOT / "src/mobile_use_mcp/android_client.py"
    inventory = PROJECT_ROOT / "provenance.toml"
    copied_inventory = tmp_path / "provenance.toml"
    copied_inventory.write_text(inventory.read_text(encoding="utf-8"), encoding="utf-8")
    copied_document = tmp_path / "PROVENANCE.md"
    copied_document.write_text(
        (PROJECT_ROOT / "PROVENANCE.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    copied_source = tmp_path / source.relative_to(PROJECT_ROOT)
    copied_source.parent.mkdir(parents=True)
    copied_source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ReleaseAuditError, match="sha256"):
        validate_source_provenance(copied_inventory, tmp_path)


def test_release_validation_can_require_named_compatibility_entries(tmp_path: Path) -> None:
    with pytest.raises(ReleaseAuditError, match="missing matrix evidence"):
        validate_compatibility_evidence(
            tmp_path,
            required_matrix_entries=("physical-huawei-api-29",),
        )
