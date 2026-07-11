"""Safe, bounded Android screen recording with segment rollover."""

import asyncio
import shutil
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from mobile_use_mcp.errors import ErrorCode, MobileUseError

ANDROID_SEGMENT_LIMIT_SECONDS = 170


class AsyncProcess(Protocol):
    @property
    def returncode(self) -> int | None: ...

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class SyncDevice(Protocol):
    def pull(self, src: str, dst: str) -> None: ...


class RecordingADBDevice(Protocol):
    @property
    def sync(self) -> SyncDevice: ...

    def shell(
        self,
        cmdargs: list[str],
        stream: bool = False,
        timeout: float | None = 600,
        encoding: str | None = "utf-8",
        rstrip: bool = True,
    ) -> str | bytes | object: ...


ProcessFactory = Callable[[str, str, int, int | None], Awaitable[AsyncProcess]]
Clock = Callable[[], float]


async def _start_adb_screenrecord(
    serial: str,
    device_path: str,
    duration_seconds: int,
    bit_rate: int | None,
) -> AsyncProcess:
    adb = shutil.which("adb")
    if adb is None:
        raise MobileUseError(
            ErrorCode.ADB_UNAVAILABLE,
            "The adb executable is not available on PATH.",
            "Install Android platform-tools and restart the MCP host.",
        )
    command = [
        adb,
        "-s",
        serial,
        "shell",
        "screenrecord",
        "--time-limit",
        str(duration_seconds),
    ]
    if bit_rate is not None:
        command.extend(["--bit-rate", str(bit_rate)])
    command.append(device_path)
    return await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


class RecordingManager:
    """Own one active recording and serialize its segment lifecycle."""

    def __init__(
        self,
        serial: str,
        adb_device: RecordingADBDevice,
        process_factory: ProcessFactory = _start_adb_screenrecord,
        clock: Clock = time.monotonic,
    ):
        self.serial = serial
        self.adb_device = adb_device
        self.process_factory = process_factory
        self.clock = clock
        self._process: AsyncProcess | None = None
        self._task: asyncio.Task[None] | None = None
        self._directory: Path | None = None
        self._segments: list[Path] = []
        self._device_path: str | None = None
        self._started_at: float | None = None
        self._max_duration_seconds = 0
        self._bit_rate: int | None = None
        self._stopping = False
        self._errors: list[str] = []

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(
        self, max_duration_seconds: int, bit_rate: int | None = None
    ) -> dict[str, object]:
        if self.active:
            raise MobileUseError(
                ErrorCode.OPERATION_FAILED,
                "An Android screen recording is already active.",
                "Call android_stop_recording before starting another recording.",
            )
        availability = await asyncio.to_thread(
            self.adb_device.shell,
            ["command", "-v", "screenrecord"],
        )
        if not isinstance(availability, str) or "screenrecord" not in availability:
            raise MobileUseError(
                ErrorCode.UNSUPPORTED,
                "This Android device does not provide the built-in screenrecord command.",
                "Use screenshots for observation or connect a device whose Android build "
                "includes screenrecord.",
                data={"serial": self.serial},
            )
        self._directory = Path(tempfile.mkdtemp(prefix="mobile-use-mcp-recording-"))
        self._segments = []
        self._errors = []
        self._started_at = self.clock()
        self._max_duration_seconds = max_duration_seconds
        self._bit_rate = bit_rate
        self._stopping = False
        await self._start_segment()
        self._task = asyncio.create_task(self._rollover_loop())
        return {
            "serial": self.serial,
            "max_duration_seconds": max_duration_seconds,
            "bit_rate": bit_rate,
        }

    async def _start_segment(self) -> None:
        assert self._started_at is not None
        elapsed = self.clock() - self._started_at
        remaining = max(1, round(self._max_duration_seconds - elapsed))
        duration = min(ANDROID_SEGMENT_LIMIT_SECONDS, remaining)
        self._device_path = f"/sdcard/mobile-use-mcp-{uuid.uuid4().hex}.mp4"
        self._process = await self.process_factory(
            self.serial,
            self._device_path,
            duration,
            self._bit_rate,
        )

    async def _pull_current_segment(self) -> None:
        if self._device_path is None or self._directory is None:
            return
        local_path = self._directory / f"segment-{len(self._segments):04d}.mp4"
        try:
            await asyncio.to_thread(self.adb_device.sync.pull, self._device_path, str(local_path))
            if local_path.exists() and local_path.stat().st_size > 0:
                self._segments.append(local_path)
            else:
                self._errors.append("A recording segment was empty.")
        except Exception as error:
            self._errors.append(f"Failed to pull a recording segment: {type(error).__name__}")
        finally:
            try:
                await asyncio.to_thread(self.adb_device.shell, ["rm", "-f", self._device_path])
            except Exception:
                self._errors.append("Failed to remove a temporary recording segment from device.")
            self._device_path = None

    async def _rollover_loop(self) -> None:
        assert self._started_at is not None
        while self._process is not None:
            await self._process.wait()
            await asyncio.sleep(0.5)
            await self._pull_current_segment()
            elapsed = self.clock() - self._started_at
            if self._stopping or elapsed >= self._max_duration_seconds:
                return
            await self._start_segment()

    async def stop(self) -> dict[str, object]:
        if self._task is None or self._started_at is None:
            raise MobileUseError(
                ErrorCode.OPERATION_FAILED,
                "No Android screen recording is active.",
                "Call android_start_recording before stopping a recording.",
            )
        self._stopping = True
        process = self._process
        if process is not None and process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
            await self._pull_current_segment()
            self._errors.append("Recording process required forced termination.")

        duration = self.clock() - self._started_at
        output_path = await self._merge_segments()
        result: dict[str, object] = {
            "duration_seconds": round(duration, 2),
            "recording_path": str(output_path) if output_path else None,
            "segment_paths": [str(path) for path in self._segments],
            "segment_count": len(self._segments),
            "warnings": self._errors.copy(),
        }
        self._reset()
        if output_path is None:
            raise MobileUseError(
                ErrorCode.OPERATION_FAILED,
                "No usable Android screen recording was captured.",
                "Keep the device unlocked and retry a shorter recording.",
                data=result,
            )
        return result

    async def _merge_segments(self) -> Path | None:
        if not self._segments:
            return None
        if len(self._segments) == 1:
            return self._segments[0]
        assert self._directory is not None
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            self._errors.append("ffmpeg is unavailable; returning separate recording segments.")
            return self._segments[-1]
        manifest = self._directory / "segments.txt"
        manifest.write_text(
            "".join(f"file '{path.name}'\n" for path in self._segments), encoding="utf-8"
        )
        output = self._directory / "recording.mp4"
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            str(manifest),
            "-c",
            "copy",
            str(output),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self._directory,
        )
        if await process.wait() == 0 and output.exists():
            return output
        self._errors.append("ffmpeg could not merge segments; returning the last segment.")
        return self._segments[-1]

    def abort(self) -> None:
        """Best-effort non-blocking cleanup for disconnect and process shutdown."""

        self._stopping = True
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
        if self._task is not None:
            self._task.cancel()
        if self._device_path is not None:
            with suppress(Exception):
                self.adb_device.shell(["rm", "-f", self._device_path])
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None
        self._reset()

    def _reset(self) -> None:
        self._process = None
        self._task = None
        self._device_path = None
        self._started_at = None
        self._stopping = False
