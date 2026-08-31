"""Create and verify the small CI lineage manifest used by compatibility evidence."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.compatibility_evidence import (  # noqa: E402
    GIT_COMMIT_RE,
    SCHEMA_VERSION,
    CompatibilityEvidenceError,
    sha256_file,
    verify_artifact_manifest,
    write_json_file,
)


def write_manifest(artifact: Path, kind: str, git_commit: str, output: Path) -> None:
    """Write a manifest that binds one artifact to the checked-out commit."""

    if kind not in {"wheel", "fixture_apk"}:
        raise CompatibilityEvidenceError("artifact kind must be wheel or fixture_apk")
    normalized_commit = git_commit.strip().casefold()
    if GIT_COMMIT_RE.fullmatch(normalized_commit) is None:
        raise CompatibilityEvidenceError("git commit must be a full 40-character SHA")
    suffix = ".whl" if kind == "wheel" else ".apk"
    if artifact.name.endswith(suffix) is False:
        raise CompatibilityEvidenceError(f"artifact must end in {suffix}")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "git_commit": normalized_commit,
        "artifact": {
            "kind": kind,
            "filename": artifact.name,
            "sha256": sha256_file(artifact),
        },
    }
    write_json_file(output, payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--kind", choices=("wheel", "fixture_apk"), required=True)
    parser.add_argument("--git-commit")
    parser.add_argument("--manifest", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.write:
            if args.git_commit is None:
                raise CompatibilityEvidenceError("--git-commit is required when writing")
            write_manifest(args.artifact, args.kind, args.git_commit, args.manifest)
            print(f"artifact manifest written: {args.artifact.name}")
        else:
            digest = sha256_file(args.artifact)
            commit = verify_artifact_manifest(
                args.manifest,
                artifact=args.artifact,
                kind=args.kind,
                expected_digest=digest,
            )
            print(f"artifact manifest verified: {args.artifact.name} ({commit})")
    except (CompatibilityEvidenceError, OSError) as error:
        print(f"artifact manifest failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
