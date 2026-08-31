from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

import scripts.android_ci_acceptance as acceptance


class _InterruptedProcess:
    pid = 4242
    returncode: int | None = None

    def wait(self, timeout: float) -> int:
        del timeout
        raise KeyboardInterrupt


def _noop_adb_cleanup(_serial: str, _package: str) -> None:
    return None


def _noop_quarantine(_artifact_root: Path, _diagnostics: Path) -> None:
    return None


def test_interactive_run_relays_only_fixed_safe_prompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    safe_prompt = (
        "USB disconnect test: unplug the selected device now, then reconnect it when prompted."
    )

    class InteractiveCompletedProcess:
        pid = 4242
        returncode = 0
        stdout = io.BytesIO(("private adapter output\n" + safe_prompt + "\n").encode())

        def wait(self, timeout: float) -> int:
            del timeout
            return 0

    def fake_popen(*_args: object, **_kwargs: object) -> InteractiveCompletedProcess:
        return InteractiveCompletedProcess()

    def fake_reap(_process: object, *, timeout_seconds: float) -> None:
        del timeout_seconds

    monkeypatch.setattr(acceptance.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(acceptance, "_terminate_and_reap", fake_reap)
    monkeypatch.setattr(acceptance, "_run_adb_cleanup", _noop_adb_cleanup)
    monkeypatch.setattr(acceptance, "_quarantine_artifacts", _noop_quarantine)

    diagnostics = tmp_path / "acceptance.log"
    assert (
        acceptance.run_acceptance(
            serial="SERIAL-UNDER-TEST",
            package="com.neoagentman.mobileusefixture",
            server_command="/tmp/mobile-use-mcp",
            artifact_root=tmp_path / "artifacts",
            diagnostics_path=diagnostics,
            exercise_usb_disconnect=True,
        )
        == 0
    )

    assert diagnostics.read_text(encoding="utf-8").startswith("private adapter output")
    displayed = capsys.readouterr().out
    assert safe_prompt in displayed
    assert "private adapter output" not in displayed


def test_timeout_gives_public_cleanup_a_chance_then_forces_and_reaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct_signals: list[object] = []
    group_signals: list[tuple[object, bool]] = []

    class TimeoutThenExit:
        pid = 4242
        returncode: int | None = None
        waits = 0

        def wait(self, timeout: float) -> int:
            self.waits += 1
            if self.waits <= 3:
                raise subprocess.TimeoutExpired("acceptance", timeout)
            self.returncode = 0
            return 0

        def send_signal(self, signum: object) -> None:
            direct_signals.append(signum)

    process = TimeoutThenExit()

    def fake_signal(_process: object, signum: object, *, force: bool = False) -> None:
        group_signals.append((signum, force))

    def fake_popen(*_args: object, **_kwargs: object) -> TimeoutThenExit:
        return process

    monkeypatch.setattr(acceptance, "_signal_process_group", fake_signal)
    monkeypatch.setattr(acceptance.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(acceptance, "_run_adb_cleanup", _noop_adb_cleanup)
    monkeypatch.setattr(acceptance, "_quarantine_artifacts", _noop_quarantine)

    assert (
        acceptance.run_acceptance(
            serial="SERIAL-UNDER-TEST",
            package="com.neoagentman.mobileusefixture",
            server_command="/tmp/mobile-use-mcp",
            artifact_root=tmp_path / "artifacts",
            diagnostics_path=tmp_path / "acceptance.log",
            total_timeout_seconds=1,
        )
        == 1
    )

    assert process.waits == 4
    assert len(direct_signals) == 1
    assert group_signals[0][1] is False
    assert group_signals[-1][1] is True


def test_keyboard_interrupt_still_runs_outer_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    process = _InterruptedProcess()

    def fake_popen(*_args: object, **_kwargs: object) -> _InterruptedProcess:
        return process

    def fake_public_cleanup(_process: object, _timeout: float) -> None:
        calls.append("public-cleanup")

    def fake_reap(_process: object, *, timeout_seconds: float) -> None:
        del timeout_seconds
        calls.append("reap")

    def fake_adb_cleanup(_serial: str, _package: str) -> None:
        calls.append("adb-cleanup")

    def fake_quarantine(_artifacts: Path, _diagnostics: Path) -> None:
        calls.append("quarantine")

    monkeypatch.setattr(acceptance.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        acceptance,
        "_request_public_cleanup",
        fake_public_cleanup,
    )
    monkeypatch.setattr(acceptance, "_terminate_and_reap", fake_reap)
    monkeypatch.setattr(acceptance, "_run_adb_cleanup", fake_adb_cleanup)
    monkeypatch.setattr(acceptance, "_quarantine_artifacts", fake_quarantine)

    with pytest.raises(KeyboardInterrupt):
        acceptance.run_acceptance(
            serial="SERIAL-UNDER-TEST",
            package="com.neoagentman.mobileusefixture",
            server_command="/tmp/mobile-use-mcp",
            artifact_root=tmp_path / "artifacts",
            diagnostics_path=tmp_path / "acceptance.log",
            total_timeout_seconds=1,
        )

    assert calls == ["public-cleanup", "reap", "adb-cleanup", "quarantine"]


def test_outer_wait_does_not_extend_the_total_business_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeouts: list[float] = []

    class CompletedProcess:
        pid = 4242
        returncode = 0

        def wait(self, timeout: float) -> int:
            timeouts.append(timeout)
            return 0

    def fake_popen(*_args: object, **_kwargs: object) -> CompletedProcess:
        return CompletedProcess()

    def fake_reap(_process: object, *, timeout_seconds: float) -> None:
        del timeout_seconds

    def fake_adb_cleanup(_serial: str, _package: str) -> None:
        return None

    def fake_quarantine(_artifacts: Path, _diagnostics: Path) -> None:
        return None

    monkeypatch.setattr(acceptance.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(acceptance, "_terminate_and_reap", fake_reap)
    monkeypatch.setattr(acceptance, "_run_adb_cleanup", fake_adb_cleanup)
    monkeypatch.setattr(acceptance, "_quarantine_artifacts", fake_quarantine)

    assert (
        acceptance.run_acceptance(
            serial="SERIAL-UNDER-TEST",
            package="com.neoagentman.mobileusefixture",
            server_command="/tmp/mobile-use-mcp",
            artifact_root=tmp_path / "artifacts",
            diagnostics_path=tmp_path / "acceptance.log",
            total_timeout_seconds=30,
        )
        == 0
    )
    assert timeouts == [30]
