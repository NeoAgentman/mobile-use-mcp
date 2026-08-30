"""Install one wheel in a throw-away environment and run the stdio smoke.

The helper deliberately creates the environment outside the checkout and runs
the smoke script from a temporary working directory.  That keeps an editable
checkout, ``PYTHONPATH``, or a local package import from masking a broken
wheel.  It is used by the package gate on every supported host.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


class WheelSmokeError(RuntimeError):
    """Raised when the artifact or its installed stdio contract is invalid."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_digest(path: Path) -> str:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != 1:
        raise WheelSmokeError(f"Digest file must contain exactly one record: {path}")
    digest = lines[0].split(maxsplit=1)[0].casefold()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise WheelSmokeError(f"Digest file does not contain a SHA-256 value: {path}")
    return digest


def _venv_python(environment: Path) -> Path:
    executable = "python.exe" if os.name == "nt" else "python"
    directory = "Scripts" if os.name == "nt" else "bin"
    result = environment / directory / executable
    if not result.is_file():
        raise WheelSmokeError(f"Virtual environment Python was not created: {result}")
    return result


def _discover_wheel(directory: Path) -> Path:
    wheels = sorted(directory.glob("*.whl"))
    if len(wheels) != 1:
        raise WheelSmokeError(f"Expected exactly one wheel in {directory}, found {len(wheels)}")
    return wheels[0]


def run_wheel_smoke(wheel: Path | None, digest_file: Path, directory: Path = Path("dist")) -> None:
    """Verify and install ``wheel``, then invoke its installed console script."""

    wheel = (wheel or _discover_wheel(directory)).resolve()
    digest_file = digest_file.resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise WheelSmokeError(f"Wheel does not exist: {wheel}")
    expected = _expected_digest(digest_file)
    actual = _sha256(wheel)
    if actual != expected:
        raise WheelSmokeError(
            f"Wheel digest mismatch for {wheel.name}: expected {expected}, found {actual}"
        )

    smoke_script = Path(__file__).with_name("stdio_smoke.py").resolve()
    with tempfile.TemporaryDirectory(prefix="mobile-use-mcp-wheel-smoke-") as temporary:
        root = Path(temporary)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = _venv_python(environment)
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                str(wheel),
            ],
            cwd=root,
            check=True,
            timeout=180,
        )

        command_name = "mobile-use-mcp.exe" if os.name == "nt" else "mobile-use-mcp"
        command = environment / ("Scripts" if os.name == "nt" else "bin") / command_name
        if not command.is_file():
            raise WheelSmokeError(f"Wheel did not install its console entry point: {command}")

        child_environment = os.environ.copy()
        child_environment.pop("PYTHONPATH", None)
        subprocess.run(
            [str(python), str(smoke_script), "--command", str(command)],
            cwd=root,
            env=child_environment,
            check=True,
            timeout=60,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--directory", type=Path, default=Path("dist"))
    parser.add_argument("--digest-file", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        run_wheel_smoke(args.wheel, args.digest_file, args.directory)
    except (OSError, subprocess.SubprocessError, WheelSmokeError) as error:
        print(f"wheel smoke failed: {error}", file=sys.stderr)
        return 1
    wheel = args.wheel or _discover_wheel(args.directory)
    print(f"wheel smoke passed for {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
