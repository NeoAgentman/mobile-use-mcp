"""Record or verify the SHA-256 digest of exactly one wheel artifact."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_wheel(directory: Path) -> Path:
    wheels = sorted(directory.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError(f"expected exactly one wheel in {directory}, found {len(wheels)}")
    return wheels[0]


def write_digest(wheel: Path, destination: Path) -> None:
    destination.write_text(f"{sha256(wheel)}  {wheel.name}\n", encoding="utf-8")


def verify_digest(wheel: Path, digest_file: Path) -> None:
    lines = [
        line.strip()
        for line in digest_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(lines) != 1:
        raise ValueError(f"digest file must contain exactly one record: {digest_file}")
    fields = lines[0].split(maxsplit=1)
    if len(fields) != 2 or fields[0] != sha256(wheel):
        actual = sha256(wheel)
        expected = fields[0] if fields else "<missing>"
        raise ValueError(f"digest mismatch: expected {expected}, found {actual}")
    recorded_name = fields[1].removeprefix("*")
    if Path(recorded_name).name != wheel.name:
        raise ValueError(f"digest file names {recorded_name!r}, not {wheel.name!r}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        wheel = args.wheel or discover_wheel(args.directory or Path("dist"))
        if args.write:
            write_digest(wheel, args.output)
        else:
            verify_digest(wheel, args.output)
    except (OSError, ValueError) as error:
        print(f"artifact digest failed: {error}", file=sys.stderr)
        return 1
    print(f"artifact digest {'written' if args.write else 'verified'}: {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
