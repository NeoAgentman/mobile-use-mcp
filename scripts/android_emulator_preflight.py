"""Run bounded, non-mutating preflight checks for the Android acceptance job.

The fixture installation is performed by the workflow.  This command checks
the exact device selected by the workflow before the MCP server is started and
emits only stage names and a one-way serial digest on failure.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys

_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")


class PreflightError(RuntimeError):
    """Raised when one required emulator capability is unavailable."""

    def __init__(self, stage: str, serial: str) -> None:
        self.stage = stage
        self.serial_digest = hashlib.sha256(serial.encode("utf-8")).hexdigest()[:16]
        super().__init__(f"{stage} (device {self.serial_digest})")


def _adb_command(adb: str, serial: str, *arguments: str) -> list[str]:
    return [adb, "-s", serial, *arguments]


def _run_adb(adb: str, serial: str, stage: str, *arguments: str) -> str:
    try:
        result = subprocess.run(
            _adb_command(adb, serial, *arguments),
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        del error
        raise PreflightError(stage, serial) from None
    if result.returncode != 0:
        raise PreflightError(stage, serial)
    return result.stdout.strip()


def _check_adb(adb: str, serial: str) -> None:
    try:
        result = subprocess.run(
            [adb, "version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        del error
        raise PreflightError("adb", serial) from None
    if result.returncode != 0 or not result.stdout.startswith("Android Debug Bridge"):
        raise PreflightError("adb", serial)


def _check_device(adb: str, serial: str) -> None:
    state = _run_adb(adb, serial, "get-state")
    if state != "device":
        raise PreflightError("device", serial)
    boot_completed = _run_adb(adb, serial, "shell", "getprop", "sys.boot_completed")
    if boot_completed != "1":
        raise PreflightError("device", serial)


def _check_uiautomator2(serial: str) -> None:
    try:
        import uiautomator2

        device = uiautomator2.connect(serial)
        info = device.info
    except Exception as error:
        del error
        raise PreflightError("uiautomator2", serial) from None
    if not isinstance(info, dict) or not info:
        raise PreflightError("uiautomator2", serial)


def _check_fixture(adb: str, serial: str, package: str) -> None:
    output = _run_adb(adb, serial, "shell", "pm", "path", package)
    if not output.startswith("package:"):
        raise PreflightError("fixture", serial)


def run_preflight(serial: str, package: str) -> None:
    """Check ADB, boot state, uiautomator2, and fixture installation."""

    if not serial.strip():
        raise ValueError("serial must not be empty")
    if _PACKAGE_RE.fullmatch(package) is None:
        raise ValueError("package must be an Android package name")
    adb = shutil.which("adb")
    if adb is None:
        raise PreflightError("adb", serial)
    _check_adb(adb, serial)
    _check_device(adb, serial)
    _check_uiautomator2(serial)
    _check_fixture(adb, serial, package)
    print("android preflight passed: adb device uiautomator2 fixture")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="Exact authorized emulator serial.")
    parser.add_argument("--package", required=True, help="Installed fixture package name.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        run_preflight(args.serial, args.package)
    except PreflightError as error:
        print(f"android preflight failed: {error}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"android preflight failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
