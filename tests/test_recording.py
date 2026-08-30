import asyncio
import sys
from pathlib import Path

import pytest

from mobile_use_mcp.config import RuntimeConfig
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.recording import RecordingManager


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.finished = asyncio.Event()
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        await self.finished.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0
        self.finished.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.finished.set()


class StubbornProcess(FakeProcess):
    def terminate(self) -> None:
        self.terminated = True


class FakeSync:
    def pull(self, src: str, dst: str) -> None:
        Path(dst).write_bytes(b"mp4-data")


class FailingSync(FakeSync):
    def pull(self, src: str, dst: str) -> None:
        raise RuntimeError("pull failed")


class FakeADB:
    def __init__(self) -> None:
        self.sync = FakeSync()
        self.shell_calls: list[list[str]] = []

    def shell(
        self,
        cmdargs: list[str],
        stream: bool = False,
        timeout: float | None = 600,
        encoding: str | None = "utf-8",
        rstrip: bool = True,
    ) -> str:
        self.shell_calls.append(cmdargs)
        if cmdargs == ["command", "-v", "screenrecord"]:
            return "/system/bin/screenrecord"
        return ""


async def test_recording_start_and_stop_returns_local_segment() -> None:
    process = FakeProcess()
    calls: list[tuple[str, str, int, int | None]] = []

    async def factory(serial: str, path: str, duration: int, bit_rate: int | None) -> FakeProcess:
        calls.append((serial, path, duration, bit_rate))
        return process

    adb = FakeADB()
    manager = RecordingManager("ABC", adb, process_factory=factory)

    started = await manager.start(60, 4_000_000)
    stopped = await manager.stop()

    assert started["max_duration_seconds"] == 60
    assert calls[0][0] == "ABC"
    assert calls[0][2:] == (60, 4_000_000)
    assert process.terminated
    assert stopped["segment_count"] == 1
    assert Path(str(stopped["recording_path"])).read_bytes() == b"mp4-data"
    assert adb.shell_calls[-1][:2] == ["rm", "-f"]


async def test_recording_rejects_duplicate_start_and_missing_stop() -> None:
    process = FakeProcess()

    async def factory(serial: str, path: str, duration: int, bit_rate: int | None) -> FakeProcess:
        return process

    manager = RecordingManager("ABC", FakeADB(), process_factory=factory)
    await manager.start(60)

    with pytest.raises(MobileUseError) as duplicate:
        await manager.start(60)
    assert duplicate.value.code == ErrorCode.OPERATION_FAILED

    await manager.stop()
    with pytest.raises(MobileUseError) as missing:
        await manager.stop()
    assert missing.value.code == ErrorCode.OPERATION_FAILED


async def test_abort_terminates_process_and_removes_device_file() -> None:
    process = FakeProcess()

    async def factory(serial: str, path: str, duration: int, bit_rate: int | None) -> FakeProcess:
        return process

    adb = FakeADB()
    manager = RecordingManager("ABC", adb, process_factory=factory)
    await manager.start(60)

    manager.abort()
    await asyncio.sleep(0)

    assert process.terminated
    assert adb.shell_calls[-1][:2] == ["rm", "-f"]


async def test_recording_reports_unsupported_device() -> None:
    adb = FakeADB()
    adb.shell = lambda *args, **kwargs: ""  # type: ignore[method-assign]
    manager = RecordingManager("ABC", adb)

    with pytest.raises(MobileUseError) as caught:
        await manager.start(60)

    assert caught.value.code == ErrorCode.UNSUPPORTED


async def test_recording_rolls_over_and_returns_segments_without_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = FakeProcess()
    first.returncode = 0
    first.finished.set()
    second = FakeProcess()
    processes = iter([first, second])
    clock = iter([0.0, 0.0, 10.0, 10.0, 20.0, 20.0])

    def missing_executable(name: str) -> str | None:
        return None

    monkeypatch.setattr("mobile_use_mcp.recording.shutil.which", missing_executable)

    async def factory(serial: str, path: str, duration: int, bit_rate: int | None) -> FakeProcess:
        return next(processes)

    manager = RecordingManager("ABC", FakeADB(), process_factory=factory, clock=lambda: next(clock))
    await manager.start(300)
    await asyncio.sleep(0.6)
    stopped = await manager.stop()

    assert stopped["segment_count"] == 2
    assert "ffmpeg is unavailable" in str(stopped["warnings"])


async def test_recording_pull_failure_is_reported_without_leaking_process() -> None:
    process = FakeProcess()

    async def factory(serial: str, path: str, duration: int, bit_rate: int | None) -> FakeProcess:
        return process

    manager = RecordingManager(
        "ABC",
        FakeADB(),
        process_factory=factory,
    )
    manager.adb_device.sync = FailingSync()  # type: ignore[assignment]

    await manager.start(60)
    with pytest.raises(MobileUseError) as caught:
        await manager.stop()

    assert process.terminated
    assert caught.value.code == ErrorCode.OPERATION_FAILED
    assert "Failed to pull a recording segment" in str(caught.value.data["warnings"])


async def test_recording_kill_fallback_cancels_stuck_rollover_task() -> None:
    process = StubbornProcess()

    async def factory(
        serial: str, path: str, duration: int, bit_rate: int | None
    ) -> StubbornProcess:
        return process

    config = RuntimeConfig(
        subprocess_timeout_seconds=0.05,
        subprocess_terminate_timeout_seconds=0.01,
    )
    manager = RecordingManager("ABC", FakeADB(), process_factory=factory, config=config)
    await manager.start(60)
    stopped = await manager.stop()

    assert process.terminated
    assert process.killed
    assert stopped["segment_count"] == 1
    assert not manager.active


async def test_recording_ffmpeg_failure_returns_last_segment_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = FakeProcess()
    first.returncode = 0
    first.finished.set()
    second = FakeProcess()
    processes = iter([first, second])
    clock = iter([0.0, 0.0, 10.0, 10.0, 20.0, 20.0])

    def fake_which(name: str) -> str | None:
        return sys.executable if name == "ffmpeg" else None

    monkeypatch.setattr("mobile_use_mcp.recording.shutil.which", fake_which)

    async def factory(serial: str, path: str, duration: int, bit_rate: int | None) -> FakeProcess:
        return next(processes)

    manager = RecordingManager("ABC", FakeADB(), process_factory=factory, clock=lambda: next(clock))
    await manager.start(300)
    await asyncio.sleep(0.6)
    stopped = await manager.stop()

    assert stopped["segment_count"] == 2
    assert "ffmpeg could not merge" in str(stopped["warnings"])
