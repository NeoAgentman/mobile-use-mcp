"""Run the Android fixture acceptance flow with process-safe CI cleanup.

The public acceptance script owns MCP-level reset, Home, recording-stop, and
disconnect cleanup. This wrapper owns the outer process group as a second line
of defence: cancellation or a job deadline terminates and reaps both the
acceptance harness and its independently installed MCP server. Diagnostics are
redacted before they are written to the path supplied by CI.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import BinaryIO

# Match absolute filesystem paths without consuming the slash in an URL.  The
# diagnostics are intentionally redacted even when a third-party dependency
# chooses a runner-specific path outside the usual hosted-runner prefixes.
_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9/:])/(?:[^/\s\"']+/)*[^/\s\"']+|"
    r"(?<![A-Za-z0-9])[A-Za-z]:\\[^\s\"']+"
)
_ENV_VALUE_RE = re.compile(r"(?i)(MOBILE_USE_[A-Z0-9_]+)=([^\s\"']+)")
_SERIAL_REPLACEMENT = "<redacted-device>"
_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)
_REAP_GRACE_SECONDS = 20.0
_SAFE_OPERATOR_PROMPTS = frozenset(
    {
        "USB disconnect test: unplug the selected device now, then reconnect it when prompted.",
        "USB disconnect observed. Reconnect the selected device now.",
    }
)


class AcceptanceRunnerError(RuntimeError):
    """Raised when the outer acceptance process cannot complete safely."""


def _capture_child_output(
    source: BinaryIO,
    destination: BinaryIO,
    *,
    relay_operator_prompts: bool,
) -> None:
    """Capture all output but relay only fixed, privacy-safe operator prompts."""

    while line := source.readline():
        destination.write(line)
        destination.flush()
        if not relay_operator_prompts:
            continue
        decoded = line.decode("utf-8", errors="replace").strip()
        if decoded in _SAFE_OPERATOR_PROMPTS:
            print(decoded, flush=True)


def _redact(text: str, serial: str) -> str:
    """Remove serials, paths, and runtime values from captured diagnostics."""

    redacted = text.replace(serial, _SERIAL_REPLACEMENT)
    redacted = _PATH_RE.sub("<redacted-path>", redacted)
    return _ENV_VALUE_RE.sub(r"\1=<redacted>", redacted)


def _signal_process_group(
    process: subprocess.Popen[bytes], signum: signal.Signals, *, force: bool = False
) -> None:
    """Signal the isolated process group, including descendants."""

    if os.name == "posix" and process.pid is not None:
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signum)
    elif force:
        with contextlib.suppress(OSError):
            process.kill()
    else:
        with contextlib.suppress(OSError):
            process.terminate()


def _terminate_and_reap(process: subprocess.Popen[bytes], timeout_seconds: float = 10) -> None:
    """Terminate an acceptance process group and wait for its child handle."""

    # Signal even after the harness handle has exited: a crashed harness can
    # otherwise leave the independently started MCP server in its process group.
    _signal_process_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _signal_process_group(process, _KILL_SIGNAL, force=True)
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            raise AcceptanceRunnerError("acceptance process could not be reaped") from error
    finally:
        # A process that exits cleanly may still have descendants.  The second
        # signal is harmless when the group no longer exists and closes that
        # leak without recursively deleting any artifact data.
        _signal_process_group(process, _KILL_SIGNAL, force=True)


def _request_public_cleanup(process: subprocess.Popen[bytes], timeout_seconds: float) -> None:
    """Give the harness a bounded chance to run its public cleanup first."""

    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            # Signal only the harness PID.  Its MCP child must remain alive
            # long enough for the harness's cancellation path to call public
            # reset/Home/disconnect before the whole process group is reaped.
            process.send_signal(signal.SIGINT)
        else:
            process.terminate()
        process.wait(timeout=timeout_seconds)
    except (OSError, subprocess.TimeoutExpired):
        return


def _run_adb_cleanup(serial: str, package: str) -> None:
    adb = shutil.which("adb")
    if adb is None:
        return
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            [adb, "-s", serial, "shell", "am", "force-stop", package],
            capture_output=True,
            check=False,
            timeout=20,
        )


def _quarantine_artifacts(artifact_root: Path, diagnostics: Path) -> None:
    """Move incomplete or secret-bearing artifacts out of the CI upload path.

    The runner temporary directory is ephemeral. Moving instead of recursively
    deleting keeps failure evidence recoverable for the lifetime of the job
    while ensuring only the redacted diagnostics file is uploaded.
    """

    if not artifact_root.exists():
        return
    quarantine = diagnostics.parent / "artifact-quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    destination = quarantine / f"run-{time.time_ns()}"
    try:
        artifact_root.rename(destination)
    except OSError:
        if not artifact_root.is_dir():
            with contextlib.suppress(OSError):
                shutil.move(str(artifact_root), str(destination))
            return
        remaining = False
        for entry in artifact_root.iterdir():
            target = destination / entry.name
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(entry), str(target))
            except OSError:
                remaining = True
        if not remaining:
            with contextlib.suppress(OSError):
                artifact_root.rmdir()


def run_acceptance(
    *,
    serial: str,
    package: str,
    server_command: str,
    artifact_root: Path,
    diagnostics_path: Path,
    stage_timeout_seconds: float = 35,
    total_timeout_seconds: float = 180,
    exercise_usb_disconnect: bool = False,
    adb_path: str | None = None,
) -> int:
    """Run the public flow, always stop/reap it, and write safe diagnostics."""

    script = Path(__file__).with_name("android_fixture_acceptance.py").resolve()
    diagnostics_path = diagnostics_path.resolve()
    diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
    raw_output = b""
    return_code = 1
    process: subprocess.Popen[bytes] | None = None
    primary_error: BaseException | None = None
    cleanup_error: AcceptanceRunnerError | None = None
    request_public_cleanup = False
    capture_thread: threading.Thread | None = None
    diagnostic_markers: list[bytes] = []
    with tempfile.TemporaryFile() as output:
        child_environment = os.environ.copy()
        # The server under test must come from the installed wheel, even when
        # a developer or hosted runner has a checkout-specific PYTHONPATH.
        child_environment.pop("PYTHONPATH", None)
        child_environment["MOBILE_USE_ARTIFACT_ROOT"] = str(artifact_root)
        command = [
            sys.executable,
            str(script),
            "--serial",
            serial,
            "--server-command",
            server_command,
            "--server-cwd",
            str(Path.cwd()),
            "--stage-timeout-seconds",
            str(stage_timeout_seconds),
            "--total-timeout-seconds",
            str(total_timeout_seconds),
        ]
        if exercise_usb_disconnect:
            command.append("--exercise-usb-disconnect")
        if adb_path is not None:
            command.extend(("--adb-path", adb_path))
        try:
            interactive_output = exercise_usb_disconnect
            process = subprocess.Popen(
                command,
                cwd=Path.cwd(),
                env=child_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if interactive_output else output,
                stderr=subprocess.STDOUT if interactive_output else output,
                start_new_session=os.name == "posix",
            )
            if interactive_output:
                if process.stdout is None:
                    raise AcceptanceRunnerError("acceptance output pipe was unavailable")
                capture_thread = threading.Thread(
                    target=_capture_child_output,
                    args=(process.stdout, output),
                    kwargs={"relay_operator_prompts": True},
                    name="android-acceptance-output",
                    daemon=True,
                )
                capture_thread.start()
            try:
                process.wait(timeout=total_timeout_seconds)
            except subprocess.TimeoutExpired:
                request_public_cleanup = True
                diagnostic_markers.append(b"android acceptance outer deadline exceeded\n")
            except BaseException as error:
                primary_error = error
                request_public_cleanup = True
                diagnostic_markers.append(b"android acceptance externally cancelled\n")
            else:
                return_code = process.returncode if process.returncode is not None else 1
        except OSError:
            diagnostic_markers.append(b"android acceptance process could not start\n")
        finally:
            if process is not None:
                graceful_seconds = max(1.0, _REAP_GRACE_SECONDS / 3)
                reap_seconds = max(1.0, _REAP_GRACE_SECONDS / 3)
                if request_public_cleanup:
                    _request_public_cleanup(process, graceful_seconds)
                try:
                    _terminate_and_reap(process, timeout_seconds=reap_seconds)
                except AcceptanceRunnerError as error:
                    cleanup_error = error
            if capture_thread is not None:
                capture_thread.join(timeout=max(1.0, _REAP_GRACE_SECONDS / 3))
                if capture_thread.is_alive() and cleanup_error is None:
                    cleanup_error = AcceptanceRunnerError(
                        "acceptance output capture could not be reaped"
                    )
            # This fallback is deliberately outside the child process.  It is
            # reached for timeout, KeyboardInterrupt, startup failure, and a
            # normal result even when the public cleanup path was interrupted.
            _run_adb_cleanup(serial, package)
            _quarantine_artifacts(artifact_root, diagnostics_path)
            for marker in diagnostic_markers:
                output.write(marker)
            output.seek(0)
            raw_output = output.read()
            diagnostics_path.write_text(
                _redact(raw_output.decode("utf-8", errors="replace"), serial),
                encoding="utf-8",
            )
    if primary_error is not None:
        raise primary_error
    if cleanup_error is not None:
        raise cleanup_error
    return return_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--server-command", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--stage-timeout-seconds", type=float, default=35)
    parser.add_argument("--total-timeout-seconds", type=float, default=180)
    parser.add_argument(
        "--exercise-usb-disconnect",
        action="store_true",
        help="Pause for an operator unplug/replug and verify public reconnect recovery.",
    )
    parser.add_argument(
        "--adb-path", help="Optional adb executable for USB disconnect observation."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.stage_timeout_seconds <= 0 or args.total_timeout_seconds <= 0:
        print("android acceptance failed: timeouts must be positive", file=sys.stderr)
        return 2
    try:
        result = run_acceptance(
            serial=args.serial,
            package=args.package,
            server_command=args.server_command,
            artifact_root=args.artifact_root,
            diagnostics_path=args.diagnostics,
            stage_timeout_seconds=args.stage_timeout_seconds,
            total_timeout_seconds=args.total_timeout_seconds,
            exercise_usb_disconnect=args.exercise_usb_disconnect,
            adb_path=args.adb_path,
        )
    except (OSError, AcceptanceRunnerError):
        print("android acceptance failed: cleanup could not complete", file=sys.stderr)
        return 1
    if result != 0:
        print(
            "android acceptance failed: see redacted diagnostics artifact",
            file=sys.stderr,
        )
        return result
    print("android acceptance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
