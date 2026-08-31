"""Audit release identity, archive contents, and upstream provenance.

The checks in this module intentionally use only the Python standard library.
They can run from a source checkout before the package is installed and can be
imported by CI or a release wrapper without depending on a particular shell.
"""

from __future__ import annotations

import argparse
import configparser
import re
import sys
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from importlib import import_module
from pathlib import Path
from typing import Any, cast

# Keep the documented direct script invocation working. Python otherwise puts
# only ``scripts/`` on ``sys.path`` and cannot resolve the sibling package.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.compatibility_evidence import (
    CompatibilityEvidenceError,
    load_matrix_config,
    sha256_file,
    validate_evidence_directory,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

__version__ = cast(str, import_module("mobile_use_mcp.version").__version__)


PACKAGE_NAME = "mobile-use-mcp"
CONSOLE_SCRIPT_NAME = "mobile-use-mcp"
CONSOLE_SCRIPT_TARGET = "mobile_use_mcp.server:main"
UPSTREAM_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ReleaseAuditError(ValueError):
    """Raised when a release identity, archive, or provenance check fails."""


def validate_compatibility_evidence(
    directory: Path,
    *,
    required_matrix_entries: tuple[str, ...] = (),
    max_age_days: int = 180,
    wheel_path: Path | None = None,
    matrix_constraints: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Validate fresh physical-device records for release use."""

    try:
        records = validate_evidence_directory(
            Path(directory),
            required_entries=required_matrix_entries,
            max_age_days=max_age_days,
            matrix_constraints=matrix_constraints,
        )
        if wheel_path is not None:
            wheel_digest = sha256_file(Path(wheel_path))
            mismatched = [
                str(record["matrix_entry"])
                for record in records
                if record["artifact_digest"]["wheel_sha256"].casefold() != wheel_digest
            ]
            if mismatched:
                raise ReleaseAuditError(
                    "Compatibility evidence references a different wheel digest for: "
                    + ", ".join(mismatched)
                )
        return records
    except CompatibilityEvidenceError as error:
        raise ReleaseAuditError(f"Compatibility evidence failed: {error}") from error


def expected_release_tag(version: str = __version__) -> str:
    """Return the canonical Git tag for a package version."""

    if not version:
        raise ReleaseAuditError("The package version cannot be empty.")
    return f"v{version}"


def validate_release_tag(tag: str, version: str = __version__) -> bool:
    """Validate a tag against the one package version source.

    Both ``v1.2.3`` and the fully qualified ``refs/tags/v1.2.3`` form are
    accepted because GitHub exposes the latter in some workflow contexts.
    """

    normalized_tag = tag.removeprefix("refs/tags/")
    expected = expected_release_tag(version)
    if normalized_tag != expected:
        raise ReleaseAuditError(
            f"Release tag {tag!r} does not match the package version; expected {expected!r}."
        )
    return True


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _normalized_wheel_name(value: str) -> str:
    return re.sub(r"[-_.]+", "_", value).casefold()


def _metadata_value(metadata: str, key: str) -> str:
    value = Parser().parsestr(metadata).get(key)
    if value is None:
        raise ReleaseAuditError(f"Artifact metadata is missing {key!r}.")
    return value.strip()


def _file_sha256(path: Path) -> str:
    try:
        return sha256_file(path)
    except CompatibilityEvidenceError as error:
        raise ReleaseAuditError(f"Could not hash file: {path.name}") from error


def _safe_project_path(root: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ReleaseAuditError(f"Provenance path must stay inside the project: {value!r}.")
    root_resolved = root.resolve()
    resolved = (root / relative).resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ReleaseAuditError(f"Provenance path escapes the project: {value!r}.")
    return resolved


def _require_text(mapping: dict[str, Any], key: str, context: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReleaseAuditError(f"{context} must contain a non-empty {key!r}.")
    return value.strip()


def _table(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseAuditError(f"{context} must be a table.")
    return cast(dict[str, Any], value)


def validate_version_configuration(root: Path = PROJECT_ROOT) -> None:
    """Ensure build metadata points at the same source used at runtime."""

    root = Path(root)
    pyproject_path = root / "pyproject.toml"
    version_path = root / "src/mobile_use_mcp/version.py"
    if not pyproject_path.is_file() or not version_path.is_file():
        raise ReleaseAuditError("Project metadata and version source must both be present.")

    try:
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ReleaseAuditError(f"Could not read pyproject.toml: {error}") from error

    project = _table(pyproject.get("project"), "pyproject.toml [project]")
    dynamic = project.get("dynamic")
    if not isinstance(dynamic, list) or "version" not in dynamic:
        raise ReleaseAuditError("Project metadata must declare version as dynamic.")
    if "version" in project:
        raise ReleaseAuditError("Project metadata must not contain a second static version.")

    tool = _table(pyproject.get("tool"), "pyproject.toml [tool]")
    hatch = _table(tool.get("hatch"), "pyproject.toml [tool.hatch]")
    hatch_version = _table(hatch.get("version"), "pyproject.toml [tool.hatch.version]")
    if hatch_version.get("path") != "src/mobile_use_mcp/version.py":
        raise ReleaseAuditError(
            "Hatchling must read the version from src/mobile_use_mcp/version.py."
        )

    version_text = version_path.read_text(encoding="utf-8")
    assignments = re.findall(r"^__version__\s*=\s*(['\"])([^'\"]+)\1\s*$", version_text, re.M)
    if len(assignments) != 1:
        raise ReleaseAuditError("The version source must define one literal __version__ value.")
    source_version = assignments[0][1]
    if source_version != __version__:
        raise ReleaseAuditError(
            f"Runtime version {__version__!r} differs from the source version {source_version!r}."
        )

    lock_path = root / "uv.lock"
    if lock_path.is_file():
        try:
            lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ReleaseAuditError(f"Could not read uv.lock: {error}") from error
        packages = lock.get("package", [])
        if not isinstance(packages, list):
            raise ReleaseAuditError("uv.lock package entries must be an array.")
        package_entries = cast(list[object], packages)
        matching = [
            cast(dict[str, Any], package)
            for package in package_entries
            if isinstance(package, dict)
            and cast(dict[str, Any], package).get("name") == PACKAGE_NAME
        ]
        if len(matching) != 1:
            raise ReleaseAuditError("uv.lock does not identify the local package.")
        lock_version = matching[0].get("version")
        if lock_version is not None and lock_version != __version__:
            raise ReleaseAuditError("uv.lock package version differs from the source version.")


def validate_source_provenance(
    inventory_path: Path,
    root: Path = PROJECT_ROOT,
) -> None:
    """Validate the machine-readable upstream inventory and source hashes."""

    inventory_path = Path(inventory_path)
    root = Path(root)
    try:
        inventory = tomllib.loads(inventory_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ReleaseAuditError(f"Provenance inventory is missing: {inventory_path}") from error
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ReleaseAuditError(f"Could not read provenance inventory: {error}") from error

    if inventory.get("schema") != 1:
        raise ReleaseAuditError("Provenance inventory schema must be 1.")
    _require_text(inventory, "upstream_project", "Provenance inventory")
    upstream_url = _require_text(inventory, "upstream_url", "Provenance inventory")
    if not upstream_url.startswith("https://"):
        raise ReleaseAuditError("Provenance upstream_url must use HTTPS.")
    revision = _require_text(inventory, "upstream_revision", "Provenance inventory")
    if UPSTREAM_REVISION_PATTERN.fullmatch(revision) is None:
        raise ReleaseAuditError("Provenance upstream_revision must be a full 40-character Git SHA.")
    legal_note = _require_text(inventory, "review_note", "Provenance inventory")
    if "not a legal" not in legal_note.casefold():
        raise ReleaseAuditError("Provenance review_note must avoid unsupported legal conclusions.")

    entries_value = inventory.get("files")
    if not isinstance(entries_value, list) or not entries_value:
        raise ReleaseAuditError("Provenance inventory must list at least one derived file.")
    entries = cast(list[object], entries_value)
    seen_paths: set[str] = set()
    for index, raw_entry in enumerate(entries):
        context = f"Provenance file entry {index + 1}"
        if not isinstance(raw_entry, dict):
            raise ReleaseAuditError(f"{context} must be a table.")
        entry = cast(dict[str, Any], raw_entry)
        path_value = _require_text(entry, "path", context)
        if path_value in seen_paths:
            raise ReleaseAuditError(f"Provenance inventory repeats {path_value!r}.")
        seen_paths.add(path_value)
        source_path = _safe_project_path(root, path_value)
        if not source_path.is_file():
            raise ReleaseAuditError(f"Provenance source file is missing: {path_value!r}.")
        _require_text(entry, "upstream_path", context)
        _require_text(entry, "kind", context)
        _require_text(entry, "modification_statement", context)
        expected_hash = _require_text(entry, "sha256", context).casefold()
        if SHA256_PATTERN.fullmatch(expected_hash) is None:
            raise ReleaseAuditError(f"{context} sha256 must be a 64-character hex digest.")
        actual_hash = _file_sha256(source_path)
        if actual_hash != expected_hash:
            raise ReleaseAuditError(
                f"Provenance sha256 for {path_value!r} is stale: expected {expected_hash}, "
                f"found {actual_hash}."
            )

    document_value = inventory.get("provenance_document", "PROVENANCE.md")
    document_name = _require_text(
        {"provenance_document": document_value}, "provenance_document", "Provenance inventory"
    )
    document = _safe_project_path(root, document_name)
    if not document.is_file():
        raise ReleaseAuditError(f"Provenance document is missing: {document_name!r}.")
    document_text = document.read_text(encoding="utf-8")
    if revision not in document_text or "not a legal conclusion" not in document_text.casefold():
        raise ReleaseAuditError(
            "PROVENANCE.md must repeat the recorded revision and the legal-review boundary."
        )
    missing_document_paths = [path for path in seen_paths if path not in document_text]
    if missing_document_paths:
        raise ReleaseAuditError(
            "PROVENANCE.md is missing inventory entries: " + ", ".join(missing_document_paths)
        )


def _wheel_dist_info_member(names: set[str], suffix: str) -> str:
    matches = sorted(name for name in names if name.endswith(suffix))
    if len(matches) != 1:
        raise ReleaseAuditError(f"Wheel must contain exactly one {suffix!r} file.")
    return matches[0]


def _validate_notice_content(filename: str, content: bytes) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReleaseAuditError(f"Artifact {filename} is not valid UTF-8 text.") from error
    required_markers = {
        "LICENSE": ("Apache License",),
        "NOTICE": ("mobile-use", "Minitap", "not a legal conclusion", "not affiliated"),
    }
    missing = [
        marker for marker in required_markers[filename] if marker.casefold() not in text.casefold()
    ]
    if missing:
        raise ReleaseAuditError(
            f"Artifact {filename} is missing required attribution markers: {', '.join(missing)}."
        )


def _validate_wheel_filename(path: Path, version: str) -> None:
    name = path.name
    prefix = f"{_normalized_wheel_name(PACKAGE_NAME)}-"
    if not name.startswith(prefix) or not name.endswith(".whl"):
        raise ReleaseAuditError(f"Wheel filename does not identify {PACKAGE_NAME!r}: {name!r}.")
    filename_version = name[len(prefix) :].split("-", 1)[0]
    if filename_version != version:
        raise ReleaseAuditError(
            f"Wheel filename version {filename_version!r} does not match {version!r}."
        )


def _validate_sdist_filename(path: Path, version: str) -> None:
    name = path.name
    suffix = ".tar.gz"
    if not name.endswith(suffix):
        raise ReleaseAuditError(f"Sdist filename does not identify {PACKAGE_NAME!r}: {name!r}.")
    stem = name[: -len(suffix)]
    filename_prefix, separator, filename_version = stem.rpartition("-")
    if not separator or _normalized_name(filename_prefix) != _normalized_name(PACKAGE_NAME):
        raise ReleaseAuditError(f"Sdist filename does not identify {PACKAGE_NAME!r}: {name!r}.")
    if filename_version != version:
        raise ReleaseAuditError(
            f"Sdist filename version {filename_version!r} does not match {version!r}."
        )


def audit_wheel(wheel_path: Path, version: str = __version__) -> None:
    """Inspect wheel metadata, entry point, and required notices."""

    wheel_path = Path(wheel_path)
    _validate_wheel_filename(wheel_path, version)
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            names = set(archive.namelist())
            metadata_member = _wheel_dist_info_member(names, ".dist-info/METADATA")
            entry_points_member = _wheel_dist_info_member(names, ".dist-info/entry_points.txt")
            try:
                metadata = archive.read(metadata_member).decode("utf-8")
            except UnicodeDecodeError as error:
                raise ReleaseAuditError("Wheel metadata is not valid UTF-8 text.") from error
            if _normalized_name(_metadata_value(metadata, "Name")) != _normalized_name(
                PACKAGE_NAME
            ):
                raise ReleaseAuditError("Wheel metadata has the wrong package name.")
            if _metadata_value(metadata, "Version") != version:
                raise ReleaseAuditError("Wheel metadata version does not match the source version.")

            entry_points = configparser.ConfigParser()
            try:
                entry_points_text = archive.read(entry_points_member).decode("utf-8")
            except UnicodeDecodeError as error:
                raise ReleaseAuditError("Wheel entry points are not valid UTF-8 text.") from error
            entry_points.read_string(entry_points_text)
            console_scripts = (
                entry_points["console_scripts"]
                if entry_points.has_section("console_scripts")
                else None
            )
            target = console_scripts.get(CONSOLE_SCRIPT_NAME) if console_scripts else None
            if target != CONSOLE_SCRIPT_TARGET:
                raise ReleaseAuditError("Wheel console entry point is missing or points elsewhere.")
            if "mobile_use_mcp/__init__.py" not in names:
                raise ReleaseAuditError("Wheel is missing the mobile_use_mcp package.")
            for notice in ("LICENSE", "NOTICE"):
                notice_members = [name for name in names if Path(name).name == notice]
                if not notice_members:
                    raise ReleaseAuditError(f"Wheel is missing required {notice} file.")
                _validate_notice_content(notice, archive.read(sorted(notice_members)[0]))
    except FileNotFoundError as error:
        raise ReleaseAuditError(f"Wheel is missing: {wheel_path}") from error
    except zipfile.BadZipFile as error:
        raise ReleaseAuditError(f"Wheel is not a valid zip archive: {wheel_path}") from error


def _sdist_member(names: set[str], filename: str) -> str:
    matches = sorted(name for name in names if Path(name).name == filename)
    if len(matches) != 1:
        raise ReleaseAuditError(f"Sdist must contain exactly one {filename!r} file.")
    return matches[0]


def audit_sdist(sdist_path: Path, version: str = __version__) -> None:
    """Inspect sdist metadata, source entry point, and required notices."""

    sdist_path = Path(sdist_path)
    _validate_sdist_filename(sdist_path, version)
    try:
        with tarfile.open(sdist_path, mode="r:gz") as archive:
            names = {member.name.removeprefix("./") for member in archive.getmembers()}
            metadata_member = _sdist_member(names, "PKG-INFO")
            metadata_file = archive.extractfile(metadata_member)
            if metadata_file is None:
                raise ReleaseAuditError("Sdist PKG-INFO cannot be read.")
            try:
                metadata = metadata_file.read().decode("utf-8")
            except UnicodeDecodeError as error:
                raise ReleaseAuditError("Sdist metadata is not valid UTF-8 text.") from error
            if _normalized_name(_metadata_value(metadata, "Name")) != _normalized_name(
                PACKAGE_NAME
            ):
                raise ReleaseAuditError("Sdist metadata has the wrong package name.")
            if _metadata_value(metadata, "Version") != version:
                raise ReleaseAuditError("Sdist metadata version does not match the source version.")

            pyproject_member = _sdist_member(names, "pyproject.toml")
            pyproject_file = archive.extractfile(pyproject_member)
            if pyproject_file is None:
                raise ReleaseAuditError("Sdist pyproject.toml cannot be read.")
            try:
                pyproject_text = pyproject_file.read().decode("utf-8")
                pyproject = tomllib.loads(pyproject_text)
            except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
                raise ReleaseAuditError("Sdist pyproject.toml is not valid UTF-8 TOML.") from error
            project = _table(pyproject.get("project"), "Sdist [project]")
            scripts = _table(project.get("scripts"), "Sdist [project.scripts]")
            if scripts.get(CONSOLE_SCRIPT_NAME) != CONSOLE_SCRIPT_TARGET:
                raise ReleaseAuditError("Sdist console entry point is missing or points elsewhere.")

            required_members = {
                "src/mobile_use_mcp/__init__.py",
                "src/mobile_use_mcp/version.py",
            }
            for required in required_members:
                if not any(name.endswith(required) for name in names):
                    raise ReleaseAuditError(f"Sdist is missing required {required} file.")
            for notice in ("LICENSE", "NOTICE"):
                notice_member = _sdist_member(names, notice)
                notice_file = archive.extractfile(notice_member)
                if notice_file is None:
                    raise ReleaseAuditError(f"Sdist {notice} cannot be read.")
                _validate_notice_content(notice, notice_file.read())
    except FileNotFoundError as error:
        raise ReleaseAuditError(f"Sdist is missing: {sdist_path}") from error
    except (tarfile.ReadError, EOFError) as error:
        raise ReleaseAuditError(f"Sdist is not a valid gzip tar archive: {sdist_path}") from error


def audit_artifacts(
    wheel_path: Path,
    sdist_path: Path,
    *,
    root: Path = PROJECT_ROOT,
    provenance_path: Path | None = None,
    tag: str | None = None,
    compatibility_evidence: Path | None = None,
    required_matrix_entries: tuple[str, ...] = (),
    compatibility_max_age_days: int = 180,
    compatibility_matrix_constraints: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Run all repeatable release checks for one wheel and one sdist.

    Compatibility evidence is opt-in for ordinary artifact checks because a
    physical-device record is operator-provided.  Once a release names an
    evidence directory (or required matrix entries), every record must be a
    fresh passing record and every named entry must be present.
    """

    root = Path(root)
    validate_version_configuration(root)
    if tag is not None:
        validate_release_tag(tag)
    if provenance_path is None:
        provenance_path = root / "provenance.toml"
    validate_source_provenance(Path(provenance_path), root)
    if compatibility_evidence is not None or required_matrix_entries:
        evidence_directory = (
            Path(compatibility_evidence)
            if compatibility_evidence is not None
            else root / "compatibility" / "evidence"
        )
        validate_compatibility_evidence(
            evidence_directory,
            required_matrix_entries=required_matrix_entries,
            max_age_days=compatibility_max_age_days,
            wheel_path=wheel_path,
            matrix_constraints=compatibility_matrix_constraints,
        )
    audit_wheel(wheel_path)
    audit_sdist(sdist_path)


def _discover_artifact(directory: Path, suffix: str) -> Path:
    matches = sorted(directory.glob(f"*{suffix}"))
    if len(matches) != 1:
        raise ReleaseAuditError(
            f"Expected exactly one {suffix} artifact in {directory}, found {len(matches)}."
        )
    return matches[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--sdist", type=Path)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument("--tag")
    parser.add_argument(
        "--compatibility-evidence",
        type=Path,
        help="Directory of privacy-safe physical-device evidence JSON records.",
    )
    parser.add_argument(
        "--require-matrix-entry",
        action="append",
        default=[],
        help="Require one fresh passing compatibility record; may be repeated.",
    )
    parser.add_argument(
        "--compatibility-matrix",
        type=Path,
        help="JSON matrix config whose required entries must be fresh and passing.",
    )
    parser.add_argument(
        "--compatibility-max-age-days",
        type=int,
        default=None,
        help="Maximum age for compatibility records (default: matrix config or 180).",
    )
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve()
        if args.tag is not None:
            validate_release_tag(args.tag)
        try:
            if args.compatibility_matrix is not None:
                configured_entries, configured_max_age, matrix_constraints = load_matrix_config(
                    args.compatibility_matrix
                )
            else:
                configured_entries, configured_max_age, matrix_constraints = (), 180, None
        except CompatibilityEvidenceError as error:
            raise ReleaseAuditError(f"Compatibility matrix failed: {error}") from error
        required_entries = tuple(configured_entries) + tuple(args.require_matrix_entry)
        if len(set(required_entries)) != len(required_entries):
            raise ReleaseAuditError("Compatibility evidence required entries must be unique.")
        max_age_days = (
            args.compatibility_max_age_days
            if args.compatibility_max_age_days is not None
            else configured_max_age
        )
        dist_dir = root / "dist"
        wheel = args.wheel or _discover_artifact(dist_dir, ".whl")
        sdist = args.sdist or _discover_artifact(dist_dir, ".tar.gz")
        provenance = args.provenance
        if provenance is None:
            provenance = root / "provenance.toml"
        audit_artifacts(
            wheel,
            sdist,
            root=root,
            provenance_path=provenance,
            tag=args.tag,
            compatibility_evidence=args.compatibility_evidence,
            required_matrix_entries=required_entries,
            compatibility_max_age_days=max_age_days,
            compatibility_matrix_constraints=(
                matrix_constraints if required_entries else None
            ),
        )
    except ReleaseAuditError as error:
        print(f"release audit failed: {error}", file=sys.stderr)
        return 1
    print(f"release audit passed for {PACKAGE_NAME} {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
