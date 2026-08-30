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

from mobile_use_mcp.config import RuntimeConfig
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.processes import (
    OwnedProcess,
    ProcessError,
    ProcessRunner,
    run_blocking,
)

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


async def _await_cancelled(task: asyncio.Task[object]) -> None:
    with suppress(BaseException):
        await task


async def _cleanup_process(
    process: AsyncProcess,
    *,
    timeout_seconds: float,
    reason: str,
) -> None:
    """Apply the shared terminate -> wait -> kill -> wait process policy."""

    if isinstance(process, OwnedProcess):
        await process.cleanup(reason=reason)
        return
    if process.returncode is not None:
        return
    with suppress(Exception):
        process.terminate()
    wait_task = asyncio.create_task(process.wait(), name="mobile-use-recording-process-wait")
    try:
        try:
            await asyncio.wait_for(asyncio.shield(wait_task), timeout_seconds)
            return
        except TimeoutError:
            with suppress(Exception):
                process.kill()
        try:
            await asyncio.wait_for(asyncio.shield(wait_task), timeout_seconds)
        except TimeoutError:
            # Keep a reaper attached even when a custom process implementation
            # ignores both signals; no caller is left waiting forever.
            wait_task.add_done_callback(_consume_process_exception)
        except Exception:
            # A custom process may report its own wait failure.  It still owns
            # the child handle, so make a best-effort kill and observe the
            # failed wait before preserving the original exception.
            with suppress(Exception):
                process.kill()
            _consume_process_exception(wait_task)
            raise
    except asyncio.CancelledError:
        # A cancelled stop request still owns the child.  Escalate once and
        # give the existing wait task one bounded chance to reap it before
        # handing cancellation back to the caller.
        with suppress(Exception):
            process.kill()
        with suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(wait_task), timeout_seconds)
        if not wait_task.done():
            wait_task.add_done_callback(_consume_process_exception)
        raise
    except Exception:
        # If the process reports a wait error before either deadline, keep the
        # ownership rule intact: signal once more, observe the failed task,
        # and preserve the original exception for the caller.
        with suppress(Exception):
            process.kill()
        _consume_process_exception(wait_task)
        raise
    finally:
        if wait_task.done():
            _consume_process_exception(wait_task)


def _consume_process_exception(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        with suppress(BaseException):
            task.exception()


async def _start_adb_screenrecord(
    serial: str,
    device_path: str,
    duration_seconds: int,
    bit_rate: int | None,
    config: RuntimeConfig | None = None,
) -> AsyncProcess:
    if config is None:
        adb = shutil.which("adb")
    elif Path(config.adb_executable).is_absolute():
        adb = config.adb_executable
    else:
        adb = shutil.which(config.adb_executable)
    if adb is None:
        raise MobileUseError(
            ErrorCode.ADB_UNAVAILABLE,
            "The adb executable is not available on PATH.",
            "Install Android platform-tools and restart the MCP host.",
        )
    command = [
        adb,
    ]
    if config is not None and (config.adb_host != "127.0.0.1" or config.adb_port != 5037):
        command.extend(["-H", config.adb_host, "-P", str(config.adb_port)])
    command.extend(
        [
            "-s",
            serial,
            "shell",
            "screenrecord",
            "--time-limit",
            str(duration_seconds),
        ]
    )
    if bit_rate is not None:
        command.extend(["--bit-rate", str(bit_rate)])
    command.append(device_path)
    runner = ProcessRunner.from_config(config) if config is not None else ProcessRunner()
    # screenrecord is intentionally long-lived.  Its diagnostic streams are
    # discarded at the process boundary so a full pipe can never stall the
    # rollover loop; lifecycle output is represented by the recording result.
    return await runner.start(command, capture_output=False)


class RecordingManager:
    """Own one active recording and serialize its segment lifecycle."""

    def __init__(
        self,
        serial: str,
        adb_device: RecordingADBDevice,
        process_factory: ProcessFactory = _start_adb_screenrecord,
        clock: Clock = time.monotonic,
        *,
        config: RuntimeConfig | None = None,
        process_runner: ProcessRunner | None = None,
    ):
        self.serial = serial
        self.adb_device = adb_device
        self.config = config or RuntimeConfig.defaults()
        uses_default_process_factory = process_factory is _start_adb_screenrecord
        process_impl: ProcessFactory
        if config is not None and process_factory is _start_adb_screenrecord:

            async def configured_process(
                record_serial: str,
                path: str,
                duration: int,
                rate: int | None,
            ) -> AsyncProcess:
                return await _start_adb_screenrecord(
                    record_serial,
                    path,
                    duration,
                    rate,
                    self.config,
                )

            process_impl = configured_process
        else:
            process_impl = process_factory
        self.process_factory = process_impl
        self.process_runner = process_runner or ProcessRunner.from_config(self.config)
        self._use_runner_for_adb_transfer = uses_default_process_factory
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
        self._close_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def _close_device(self) -> None:
        close = getattr(self.adb_device, "close", None)
        if callable(close):
            with suppress(Exception):
                close()

    async def start(
        self, max_duration_seconds: int, bit_rate: int | None = None
    ) -> dict[str, object]:
        self._loop = asyncio.get_running_loop()
        if self.active:
            raise MobileUseError(
                ErrorCode.OPERATION_FAILED,
                "An Android screen recording is already active.",
                "Call android_stop_recording before starting another recording.",
            )
        availability = await run_blocking(
            self.adb_device.shell,
            ["command", "-v", "screenrecord"],
            timeout=self.config.subprocess_timeout_seconds,
            timeout_seconds=self.config.subprocess_timeout_seconds,
            on_abort=self._close_device,
        )
        if not isinstance(availability, str) or "screenrecord" not in availability:
            raise MobileUseError(
                ErrorCode.UNSUPPORTED,
                "This Android device does not provide the built-in screenrecord command.",
                "Use screenshots for observation or connect a device whose Android build "
                "includes screenrecord.",
                data={"serial": self.serial},
            )
        try:
            self.config.artifact_root.mkdir(parents=True, exist_ok=True)
            self._directory = Path(
                tempfile.mkdtemp(
                    prefix="mobile-use-mcp-recording-",
                    dir=str(self.config.artifact_root),
                )
            )
        except OSError as error:
            raise MobileUseError(
                ErrorCode.OPERATION_FAILED,
                "The configured recording artifact directory is not writable.",
                "Choose a writable artifact root and retry recording.",
            ) from error
        self._segments = []
        self._errors = []
        self._close_task = None
        self._started_at = self.clock()
        self._max_duration_seconds = max_duration_seconds
        self._bit_rate = bit_rate
        self._stopping = False
        try:
            await self._start_segment()
        except BaseException:
            # A process may have been spawned before its factory reports a
            # startup error.  Route that partial state through the same
            # idempotent owner-close path instead of leaving a device file or
            # child behind.
            await self.aclose()
            raise
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
            await self._pull_from_device(self._device_path, local_path)
            if local_path.exists() and local_path.stat().st_size > 0:
                self._segments.append(local_path)
            else:
                self._errors.append("A recording segment was empty.")
        except Exception as error:
            self._errors.append(f"Failed to pull a recording segment: {type(error).__name__}")
        finally:
            try:
                await self._remove_device_file(self._device_path)
            except Exception:
                self._errors.append("Failed to remove a temporary recording segment from device.")
            self._device_path = None

    def _adb_command(self, *arguments: str) -> list[str]:
        executable = self.config.adb_executable
        command = [executable]
        if self.config.adb_host != "127.0.0.1" or self.config.adb_port != 5037:
            command.extend(["-H", self.config.adb_host, "-P", str(self.config.adb_port)])
        command.extend(arguments)
        return command

    async def _pull_from_device(self, device_path: str, local_path: Path) -> None:
        if not self._use_runner_for_adb_transfer:
            await run_blocking(
                self.adb_device.sync.pull,
                device_path,
                str(local_path),
                timeout_seconds=self.config.subprocess_timeout_seconds,
                on_abort=self._close_device,
            )
            return
        result = await self.process_runner.run(
            self._adb_command("-s", self.serial, "pull", device_path, str(local_path)),
            timeout_seconds=self.config.subprocess_timeout_seconds,
        )
        if not result.ok:
            raise ProcessError("adb pull returned a non-zero exit status")

    async def _remove_device_file(self, device_path: str) -> None:
        if not self._use_runner_for_adb_transfer:
            await run_blocking(
                self.adb_device.shell,
                ["rm", "-f", device_path],
                timeout=self.config.subprocess_timeout_seconds,
                timeout_seconds=self.config.subprocess_timeout_seconds,
                on_abort=self._close_device,
            )
            return
        result = await self.process_runner.run(
            self._adb_command("-s", self.serial, "shell", "rm", "-f", device_path),
            timeout_seconds=self.config.subprocess_timeout_seconds,
        )
        if not result.ok:
            raise ProcessError("adb cleanup returned a non-zero exit status")

    async def _rollover_loop(self) -> None:
        assert self._started_at is not None
        while self._process is not None:
            process = self._process
            try:
                returncode = await process.wait()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._errors.append(f"A recording segment process failed: {type(error).__name__}.")
                returncode = -1
            if returncode != 0:
                self._errors.append("A recording segment process exited unsuccessfully.")
            await asyncio.sleep(0.5)
            await self._pull_current_segment()
            elapsed = self.clock() - self._started_at
            if self._stopping or elapsed >= self._max_duration_seconds:
                return
            await self._start_segment()

    async def stop(self) -> dict[str, object]:
        try:
            return await self._stop_impl()
        except asyncio.CancelledError:
            await self.aclose()
            raise

    async def _stop_impl(self) -> dict[str, object]:
        if self._task is None or self._started_at is None:
            raise MobileUseError(
                ErrorCode.OPERATION_FAILED,
                "No Android screen recording is active.",
                "Call android_start_recording before stopping a recording.",
            )
        self._stopping = True
        process = self._process
        if process is not None:
            await _cleanup_process(
                process,
                timeout_seconds=self.config.subprocess_terminate_timeout_seconds,
                reason="recording_stop",
            )
        try:
            await asyncio.wait_for(
                asyncio.shield(self._task),
                timeout=self.config.subprocess_timeout_seconds,
            )
        except TimeoutError:
            if process is not None:
                with suppress(Exception):
                    process.kill()
            task = self._task
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
                await _await_cancelled(task)
            await self._pull_current_segment()
            self._errors.append("Recording process required forced termination.")

        duration = self.clock() - self._started_at
        try:
            output_path = await self._merge_segments()
        except asyncio.CancelledError:
            await self.aclose()
            raise
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
        try:
            result = await self.process_runner.run(
                [
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
                ],
                cwd=self._directory,
                timeout_seconds=self.config.subprocess_timeout_seconds,
            )
        except ProcessError as error:
            self._errors.append(f"ffmpeg could not merge segments: {type(error).__name__}.")
            return self._segments[-1]
        if result.ok and output.exists():
            return output
        self._errors.append("ffmpeg could not merge segments; returning the last segment.")
        return self._segments[-1]

    async def _aclose_impl(self) -> None:
        """Run the one awaitable cleanup sequence used by disconnect/shutdown."""

        if self._task is None and self._process is None and self._device_path is None:
            return
        self._stopping = True
        process = self._process
        if process is not None:
            with suppress(Exception):
                await _cleanup_process(
                    process,
                    timeout_seconds=self.config.subprocess_terminate_timeout_seconds,
                    reason="owner_close",
                )
        task = self._task
        if task is not None and task is not asyncio.current_task() and not task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=self.config.subprocess_timeout_seconds,
                )
            except TimeoutError:
                task.cancel()
                await _await_cancelled(task)
            except BaseException:
                pass
        if self._device_path is not None:
            with suppress(Exception):
                await self._remove_device_file(self._device_path)
        directory = self._directory
        self._reset()
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)

    async def aclose(self) -> None:
        """Idempotently terminate, wait, kill if needed, and reap recording work."""

        self._schedule_close()
        assert self._close_task is not None
        await asyncio.shield(self._close_task)

    def _schedule_close(self) -> asyncio.Task[None]:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._aclose_impl(), name="mobile-use-recording-close"
            )
        return self._close_task

    def abort(self) -> asyncio.Task[None] | None:
        """Schedule the awaitable cleanup sequence for sync compatibility.

        Older embedders call ``abort`` from synchronous disconnect methods.  A
        running event loop receives a task that can be awaited by newer code;
        the task is intentionally scheduled rather than detached fire-and-
        forget.  The async ``aclose`` method is the authoritative shutdown
        boundary.
        """

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is not None and not self._loop.is_closed():
                # ``DeviceSession.connect`` can release an old controller from
                # its worker thread.  Submit to the loop that owns the
                # asyncio subprocess instead of trying to await it from a
                # foreign loop (which would leave the child unreaped).
                close_future = asyncio.run_coroutine_threadsafe(self.aclose(), self._loop)
                try:
                    close_future.result(
                        timeout=(
                            self.config.subprocess_timeout_seconds
                            + 2 * self.config.subprocess_terminate_timeout_seconds
                        )
                    )
                except TimeoutError:
                    close_future.cancel()
                except Exception:
                    # Synchronous callers cannot receive the async cleanup
                    # error; the process boundary has still made a bounded
                    # terminate/kill/reap attempt.
                    pass
                return None
            self._stopping = True
            process = self._process
            if process is not None and process.returncode is None:
                with suppress(Exception):
                    process.terminate()
            if self._task is not None:
                self._task.cancel()
            if self._device_path is not None:
                with suppress(Exception):
                    self.adb_device.shell(["rm", "-f", self._device_path])
            directory = self._directory
            self._reset()
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)
            return None
        # Preserve the historical synchronous side effect for callers that
        # cannot await ``abort``.  The process signal/reap and task join still
        # happen through the scheduled authoritative cleanup task.
        if self._device_path is not None:
            with suppress(Exception):
                self.adb_device.shell(
                    ["rm", "-f", self._device_path],
                    timeout=self.config.subprocess_timeout_seconds,
                )
            self._device_path = None
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None
        return self._schedule_close()

    def _reset(self) -> None:
        self._process = None
        self._task = None
        self._device_path = None
        self._started_at = None
        self._stopping = False
