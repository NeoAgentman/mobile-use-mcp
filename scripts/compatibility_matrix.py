"""Validate and render the Android compatibility evidence matrix."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# See the equivalent bootstrap in the physical runner: this keeps the
# documented ``python scripts/compatibility_matrix.py`` invocation working.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.compatibility_evidence import (
    CompatibilityEvidenceError,
    load_matrix_config,
    update_markdown_matrix,
    validate_evidence_directory,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--evidence-dir",
            type=Path,
            required=True,
            help="Directory containing one JSON file per matrix entry.",
        )
        command.add_argument(
            "--require-matrix-entry",
            action="append",
            default=[],
            help="Require a fresh passing entry; may be repeated.",
        )
        command.add_argument(
            "--matrix-config",
            type=Path,
            help="Optional JSON file naming all required release entries.",
        )
        command.add_argument(
            "--max-age-days",
            type=int,
            default=None,
            help="Override the matrix freshness window (default: matrix config or 180 days).",
        )

    validate = subparsers.add_parser("validate", help="Validate records for release use.")
    common(validate)

    render = subparsers.add_parser("render", help="Render records into a Markdown matrix.")
    common(render)
    render.add_argument("--output", type=Path, required=True, help="Markdown document to update.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.matrix_config is not None:
            configured_entries, configured_max_age, matrix_constraints = load_matrix_config(
                args.matrix_config
            )
        else:
            configured_entries, configured_max_age, matrix_constraints = (), 180, None
        required_entries = tuple(configured_entries) + tuple(args.require_matrix_entry)
        if len(set(required_entries)) != len(required_entries):
            raise CompatibilityEvidenceError("required matrix entries must be unique")
        max_age_days = (
            args.max_age_days if args.max_age_days is not None else configured_max_age
        )
        records = validate_evidence_directory(
            args.evidence_dir,
            # Rendering is useful before the full matrix is complete: it
            # shows missing entries as gaps. The validate command is the
            # release gate that requires every named entry.
            required_entries=required_entries if args.command == "validate" else (),
            max_age_days=max_age_days,
            matrix_constraints=(
                matrix_constraints if args.command == "validate" else None
            ),
        )
        if args.command == "render":
            update_markdown_matrix(
                args.output,
                records,
                required_entries=required_entries,
            )
    except CompatibilityEvidenceError as error:
        print(f"compatibility matrix failed: {error}")
        return 1
    print(f"compatibility matrix {args.command} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
