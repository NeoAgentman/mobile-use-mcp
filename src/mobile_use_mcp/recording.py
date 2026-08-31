"""Safe, bounded Android screen recording with segment rollover."""

import asyncio
import json
import re
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from mobile_use_mcp.config import RuntimeConfig
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import RecordingArtifact, RecordingSegment, RecordingState
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
WallClock = Callable[[], datetime]

_ARTIFACT_ID_PATTERN = r"artifact-[0-9a-f]{32}"
_ARTIFACT_ID_RE = re.compile(rf"\A{_ARTIFACT_ID_PATTERN}\Z")


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
    """Own one Android recording and its retained host-local artifacts.

    The manager is intentionally the only component that knows device-side
    temporary paths or artifact directories.  Public callers exchange opaque
    artifact IDs and metadata; cleanup derives paths from the manager's own
    registry and never accepts a client-supplied local path.
    """

    def __init__(
        self,
        serial: str,
        adb_device: RecordingADBDevice,
        process_factory: ProcessFactory = _start_adb_screenrecord,
        clock: Clock = time.monotonic,
        *,
        config: RuntimeConfig | None = None,
        process_runner: ProcessRunner | None = None,
        wall_clock: WallClock | None = None,
        session_id: str | None = None,
        generation: int | None = None,
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
        self.wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._process: AsyncProcess | None = None
        self._task: asyncio.Task[None] | None = None
        self._directory: Path | None = None
        self._segments: list[Path] = []
        self._segment_metadata: list[RecordingSegment] = []
        self._segment_started_at: datetime | None = None
        self._device_path: str | None = None
        self._started_at: float | None = None
        self._started_at_wall: datetime | None = None
        self._max_duration_seconds = 0
        self._bit_rate: int | None = None
        self._stopping = False
        self._errors: list[str] = []
        self._close_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._operation_lock: asyncio.Lock | None = None

        self._state = RecordingState.IDLE
        self._session_id = session_id
        self._generation = generation
        self._artifact_id: str | None = None
        self._artifact: RecordingArtifact | None = None
        self._artifacts: dict[str, RecordingArtifact] = {}
        self._artifact_directories: dict[str, Path] = {}
        self._artifact_expiry_monotonic: dict[str, float] = {}
        self._expired_artifact_ids: set[str] = set()
        self._failure: MobileUseError | None = None
        self._load_retained_artifacts()

    def _lock(self) -> asyncio.Lock:
        if self._operation_lock is None:
            self._operation_lock = asyncio.Lock()
        return self._operation_lock

    @property
    def state(self) -> RecordingState:
        self._prune_expired()
        return self._state

    @property
    def recording_state(self) -> RecordingState:
        """Compatibility alias for callers that name the state explicitly."""

        return self.state

    @property
    def active(self) -> bool:
        return self._state in {RecordingState.RECORDING, RecordingState.STOPPING}

    @property
    def artifact_id(self) -> str | None:
        self._prune_expired()
        return self._artifact_id

    @property
    def artifact(self) -> RecordingArtifact | None:
        self._prune_expired()
        artifact = self._artifact
        return artifact.model_copy(deep=True) if artifact is not None else None

    @property
    def artifacts(self) -> list[RecordingArtifact]:
        """Return retained artifacts in deterministic completion order."""

        self._prune_expired()
        return [
            artifact.model_copy(deep=True)
            for artifact in sorted(
                self._artifacts.values(),
                key=lambda item: (item.completed_at, item.artifact_id),
            )
        ]

    def bind_session(self, session_id: str | None, generation: int | None) -> None:
        """Stamp future recordings with the current DeviceSession identity."""

        if self.active:
            return
        self._session_id = session_id
        self._generation = generation

    # Alternate names used by controller/session embedders.
    set_context = bind_session
    bind_context = bind_session

    def _close_device(self) -> None:
        close = getattr(self.adb_device, "close", None)
        if callable(close):
            with suppress(Exception):
                close()

    def _now(self) -> datetime:
        value = self.wall_clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _root(self) -> Path:
        root = self.config.artifact_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _under(path: Path, parent: Path) -> bool:
        try:
            path.resolve().relative_to(parent.resolve())
        except ValueError:
            return False
        return True

    def _new_artifact_directory(self, artifact_id: str) -> Path:
        root = self._root()
        directory = (root / artifact_id).resolve()
        if directory.parent != root:
            raise MobileUseError(
                ErrorCode.OPERATION_FAILED,
                "The recording artifact path could not be contained below its configured root.",
                "Choose a normal writable artifact root and retry recording.",
            )
        directory.mkdir()
        return directory

    def _new_artifact_id(self) -> str:
        # uuid4 is generated by this process; callers never provide IDs or
        # paths that can influence cleanup.
        return f"artifact-{uuid.uuid4().hex}"

    def _load_retained_artifacts(self) -> None:
        """Recover manager-owned metadata after a process restart.

        Only a directory with the generated artifact name and a valid metadata
        document is considered owned. Arbitrary files below the configured
        root are ignored and are never removed by retention cleanup.
        """

        try:
            root = self._root()
            entries = list(root.iterdir())
        except OSError:
            return
        for directory in entries:
            if not directory.is_dir() or _ARTIFACT_ID_RE.fullmatch(directory.name) is None:
                continue
            metadata_path = directory / "metadata.json"
            try:
                raw = json.loads(metadata_path.read_text(encoding="utf-8"))
                artifact = RecordingArtifact.model_validate(raw)
            except (OSError, ValueError, TypeError):
                continue
            if artifact.artifact_id != directory.name:
                continue
            # A shared artifact root may be used by more than one configured
            # device.  Never make one device's recording addressable through a
            # manager for another device.
            if artifact.serial != self.serial:
                continue
            if any(
                timestamp.tzinfo is None
                for timestamp in (artifact.started_at, artifact.completed_at, artifact.expires_at)
            ):
                continue
            resolved_directory = directory.resolve()
            if resolved_directory.parent != root or not self._artifact_paths_contained(
                artifact, resolved_directory
            ):
                continue
            if not Path(artifact.recording_path).is_file():
                continue
            self._artifacts[artifact.artifact_id] = artifact
            self._artifact_directories[artifact.artifact_id] = resolved_directory

        # A completed unclaimed artifact is still the current recording after
        # a process restart.  Pick the newest one deterministically and keep
        # older retained artifacts available by ID.  Claimed state is persisted
        # by _persist_artifact, so a restart cannot accidentally turn a claimed
        # artifact back into an overwrite-preventing ready artifact.
        self._prune_expired()
        unclaimed = [artifact for artifact in self._artifacts.values() if not artifact.claimed]
        if unclaimed:
            current = max(unclaimed, key=lambda item: (item.completed_at, item.artifact_id))
            self._artifact = current
            self._artifact_id = current.artifact_id
            self._state = RecordingState.READY
            self._enforce_pressure(protected={current.artifact_id})
        else:
            self._enforce_pressure()

    def _artifact_paths_contained(self, artifact: RecordingArtifact, directory: Path) -> bool:
        root = self._root()
        paths = [Path(artifact.path), Path(artifact.recording_path), Path(artifact.artifact_path)]
        paths.extend(Path(path) for path in artifact.segment_paths)
        paths.extend(Path(segment.path) for segment in artifact.segments)
        return all(self._under(path, directory) and self._under(path, root) for path in paths)

    def _persist_artifact(self, artifact: RecordingArtifact) -> None:
        """Persist metadata only inside the manager-registered artifact directory."""

        directory = self._artifact_directories.get(artifact.artifact_id)
        if directory is None:
            raise OSError("recording artifact directory is not registered")
        root = self._root()
        if directory.parent != root or not self._under(directory, root):
            raise OSError("recording artifact directory escaped configured root")
        metadata_path = (directory / "metadata.json").resolve()
        if not self._under(metadata_path, directory):
            raise OSError("recording metadata path escaped artifact directory")
        metadata_path.write_text(
            json.dumps(artifact.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

    def _recording_error(
        self,
        code: ErrorCode,
        message: str,
        suggestion: str,
        *,
        data: dict[str, object] | None = None,
    ) -> MobileUseError:
        return MobileUseError(code, message, suggestion, data=data or {})

    def _set_failed(self, error: MobileUseError) -> None:
        self._state = RecordingState.FAILED
        self._failure = error

    def _result(
        self,
        *,
        state: RecordingState | None = None,
        success: bool = True,
        operation_id: str | None = None,
        artifact: RecordingArtifact | None = None,
        retrieved: bool = False,
    ) -> dict[str, object]:
        event_state = state or self._state
        selected = artifact or (self._artifact if self._state == RecordingState.READY else None)
        selected_payload = selected.model_dump(mode="json") if selected is not None else None
        warnings = list(self._errors)
        if selected is not None:
            warnings = list(selected.warnings)
        payload: dict[str, object] = {
            "success": success,
            "operation_id": operation_id or f"op-{uuid.uuid4().hex}",
            "state": event_state.value,
            "serial": self.serial,
            "session_id": self._session_id,
            "generation": self._generation,
            "artifact_id": selected.artifact_id if selected else self._artifact_id,
            "artifact": selected_payload,
            "max_duration_seconds": self._max_duration_seconds or None,
            "bit_rate": self._bit_rate,
            "duration_seconds": selected.duration_seconds if selected else None,
            "recording_path": selected.recording_path if selected else None,
            "segment_paths": list(selected.segment_paths) if selected else [],
            "segment_count": selected.segment_count if selected else len(self._segments),
            "warnings": warnings[:20],
            "retrieved": retrieved,
        }
        if self._failure is not None and (not success or event_state == RecordingState.FAILED):
            payload.update(
                {
                    "error_code": self._failure.code.value,
                    "category": self._failure.category.value,
                    "retryable": self._failure.retryable,
                    "message": self._failure.message,
                    "suggestion": self._failure.suggestion,
                    "details": dict(self._failure.data),
                }
            )
        else:
            payload["message"] = (
                "Android screen recording is ready."
                if event_state == RecordingState.READY
                else (
                    "Android screen recording failed."
                    if event_state == RecordingState.FAILED
                    else (
                        "Android screen recording is stopping."
                        if event_state == RecordingState.STOPPING
                        else (
                            "No Android screen recording is active."
                            if event_state == RecordingState.IDLE
                            else "Android screen recording is active."
                        )
                    )
                )
            )
        payload["data"] = {
            key: value
            for key, value in payload.items()
            if key
            in {
                "serial",
                "max_duration_seconds",
                "bit_rate",
                "duration_seconds",
                "recording_path",
                "segment_paths",
                "segment_count",
                "warnings",
                "artifact_id",
                "artifact",
                "state",
            }
        }
        return payload

    async def start(
        self,
        max_duration_seconds: int,
        bit_rate: int | None = None,
    ) -> dict[str, object]:
        self._loop = asyncio.get_running_loop()
        async with self._lock():
            self._prune_expired()
            if self.active:
                raise self._recording_error(
                    ErrorCode.RECORDING_NOT_ACTIVE,
                    "An Android screen recording is already active.",
                    "Call android_stop_recording before starting another recording.",
                    data={"state": self._state.value, "artifact_id": self._artifact_id},
                )
            if self._state == RecordingState.READY and self._artifact is not None:
                raise self._recording_error(
                    ErrorCode.RECORDING_ARTIFACT_UNCLAIMED,
                    "A completed Android recording is ready and has not been retrieved.",
                    "Call android_retrieve_recording with the returned artifact_id first.",
                    data={"artifact_id": self._artifact.artifact_id},
                )

            # A failed attempt may be retried, but its incomplete files are
            # never carried into the new artifact.
            if self._directory is not None:
                await self._cleanup_incomplete("recording_restart")
            self._failure = None
            self._errors = []
            self._segments = []
            self._segment_metadata = []
            self._artifact = None
            self._artifact_id = self._new_artifact_id()
            try:
                availability = await run_blocking(
                    self.adb_device.shell,
                    ["command", "-v", "screenrecord"],
                    timeout=self.config.subprocess_timeout_seconds,
                    timeout_seconds=self.config.subprocess_timeout_seconds,
                    on_abort=self._close_device,
                )
                if not isinstance(availability, str) or "screenrecord" not in availability:
                    raise self._recording_error(
                        ErrorCode.UNSUPPORTED,
                        "This Android device does not provide the built-in screenrecord command.",
                        "Use screenshots for observation or connect a device whose Android build "
                        "includes screenrecord.",
                        data={"serial": self.serial},
                    )
                assert self._artifact_id is not None
                self._directory = self._new_artifact_directory(self._artifact_id)
                self._started_at = self.clock()
                self._started_at_wall = self._now()
                self._max_duration_seconds = max_duration_seconds
                self._bit_rate = bit_rate
                self._stopping = False
                self._state = RecordingState.RECORDING
                self._close_task = None
                await self._start_segment()
            except BaseException as error:
                if isinstance(error, MobileUseError):
                    failure = error
                elif isinstance(error, asyncio.CancelledError):
                    failure = self._recording_error(
                        ErrorCode.CANCELLED,
                        "Android screen recording start was cancelled.",
                        "Inspect android_status before retrying the recording.",
                    )
                else:
                    failure = self._recording_error(
                        ErrorCode.RECORDING_FAILED,
                        "Android screen recording could not be started.",
                        "Check the device and artifact root, then retry recording.",
                    )
                self._set_failed(failure)
                await self._cleanup_incomplete("recording_start_failure")
                raise
            self._task = asyncio.create_task(self._rollover_loop(), name="mobile-use-recording")
            return self._result(state=RecordingState.RECORDING)

    async def _start_segment(self) -> None:
        assert self._started_at is not None
        elapsed = self.clock() - self._started_at
        remaining = max(1, round(self._max_duration_seconds - elapsed))
        duration = min(ANDROID_SEGMENT_LIMIT_SECONDS, remaining)
        self._device_path = f"/sdcard/mobile-use-mcp-{uuid.uuid4().hex}.mp4"
        self._segment_started_at = self._now()
        self._process = await self.process_factory(
            self.serial,
            self._device_path,
            duration,
            self._bit_rate,
        )

    async def _pull_current_segment(self) -> None:
        if self._device_path is None or self._directory is None:
            return
        device_path = self._device_path
        local_path = self._directory / f"segment-{len(self._segments):04d}.mp4"
        started_at = self._segment_started_at or self._now()
        completed_at = self._now()
        try:
            await self._pull_from_device(device_path, local_path)
            if local_path.exists() and local_path.stat().st_size > 0:
                if not self._under(local_path, self._directory):
                    raise ProcessError("recording segment escaped its owned directory")
                self._segments.append(local_path)
                self._segment_metadata.append(
                    RecordingSegment(
                        index=len(self._segment_metadata),
                        path=str(local_path),
                        started_at=started_at,
                        completed_at=completed_at,
                        duration_seconds=max(0.0, (completed_at - started_at).total_seconds()),
                        size_bytes=local_path.stat().st_size,
                    )
                )
            else:
                self._errors.append("A recording segment was empty.")
        except Exception as error:
            self._errors.append(f"Failed to pull a recording segment: {type(error).__name__}")
        finally:
            try:
                await self._remove_device_file(device_path)
            except Exception:
                self._errors.append("Failed to remove a temporary recording segment from device.")
            self._device_path = None
            self._segment_started_at = None

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
            before = len(self._segments)
            await self._pull_current_segment()
            elapsed = self.clock() - self._started_at
            # A non-zero child or failed transfer terminates the recording. A
            # previously pulled segment remains valid and can still become a
            # ready fallback artifact.
            transfer_failed = len(self._segments) == before
            if self._stopping or elapsed >= self._max_duration_seconds or returncode != 0:
                try:
                    await self._finish_after_segment()
                except asyncio.CancelledError:
                    raise
                except MobileUseError:
                    # The failed state and incomplete cleanup are committed
                    # by _finish_after_segment; natural completion has no
                    # caller task to which the business error can be raised.
                    pass
                return
            if transfer_failed:
                try:
                    await self._finish_after_segment()
                except asyncio.CancelledError:
                    raise
                except MobileUseError:
                    pass
                return
            try:
                await self._start_segment()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._errors.append(f"Failed to start a recording segment: {type(error).__name__}")
                try:
                    await self._finish_after_segment()
                except asyncio.CancelledError:
                    raise
                except MobileUseError:
                    pass
                return

    async def _finish_after_segment(self) -> RecordingArtifact | None:
        self._state = RecordingState.STOPPING
        self._stopping = True
        try:
            output_path = await self._merge_segments()
        except asyncio.CancelledError:
            raise
        try:
            output_is_usable = (
                output_path is not None and output_path.is_file() and output_path.stat().st_size > 0
            )
        except OSError:
            output_is_usable = False
        if not output_is_usable:
            failure = self._recording_error(
                ErrorCode.RECORDING_FAILED,
                "No usable Android screen recording was captured.",
                "Keep the device unlocked and retry a shorter recording.",
                data={
                    "warnings": list(self._errors),
                    "segment_paths": [str(path) for path in self._segments],
                    "segment_count": len(self._segments),
                },
            )
            self._set_failed(failure)
            await self._cleanup_incomplete("recording_finalize_failure")
            raise failure
        assert output_path is not None
        assert self._artifact_id is not None
        assert self._directory is not None
        root = self._root()
        resolved_output = output_path.resolve()
        if not self._under(resolved_output, root) or not self._under(
            resolved_output, self._directory
        ):
            failure = self._recording_error(
                ErrorCode.RECORDING_FAILED,
                "The recording output escaped its owned artifact directory.",
                "Choose a normal artifact root and retry recording.",
            )
            self._set_failed(failure)
            await self._cleanup_incomplete("recording_path_failure")
            raise failure
        started_at = self._started_at_wall or self._now()
        completed_at = self._now()
        started_monotonic = self._started_at
        assert started_monotonic is not None
        completed_monotonic = self.clock()
        duration = max(0.0, completed_monotonic - started_monotonic)
        expires_at = completed_at + timedelta(seconds=self.config.artifact_retention_seconds)
        try:
            output_size = resolved_output.stat().st_size
            if output_size > self.config.artifact_max_bytes:
                failure = self._recording_error(
                    ErrorCode.RECORDING_FAILED,
                    "The completed Android recording exceeds the artifact byte budget.",
                    "Retry with a shorter duration or lower bit rate.",
                    data={
                        "actual_bytes": output_size,
                        "max_bytes": self.config.artifact_max_bytes,
                    },
                )
                self._set_failed(failure)
                await self._cleanup_incomplete("recording_artifact_too_large")
                raise failure
            artifact = RecordingArtifact(
                artifact_id=self._artifact_id,
                serial=self.serial,
                session_id=self._session_id,
                generation=self._generation,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=round(duration, 2),
                size_bytes=output_size,
                path=str(resolved_output),
                recording_path=str(resolved_output),
                artifact_path=str(resolved_output),
                segment_paths=[str(path.resolve()) for path in self._segments],
                segment_count=len(self._segments),
                segments=list(self._segment_metadata),
                expires_at=expires_at,
                claimed=False,
                warnings=list(self._errors)[:20],
            )
        except (OSError, ValueError, TypeError) as error:
            failure = self._recording_error(
                ErrorCode.RECORDING_FAILED,
                "The completed Android recording metadata could not be created.",
                "Retry the recording after checking the configured artifact root.",
                data={"reason": type(error).__name__},
            )
            self._set_failed(failure)
            await self._cleanup_incomplete("recording_metadata_failure")
            raise failure from error
        self._artifacts[artifact.artifact_id] = artifact
        self._artifact_directories[artifact.artifact_id] = self._directory
        self._artifact_expiry_monotonic[artifact.artifact_id] = (
            completed_monotonic + self.config.artifact_retention_seconds
        )
        try:
            self._persist_artifact(artifact)
        except OSError:
            # Metadata is useful for restart recovery but not required to
            # serve the in-memory artifact. Keep the completed media ready and
            # make the loss explicit to the caller.
            self._errors.append("Failed to persist recording artifact metadata.")
            artifact = artifact.model_copy(update={"warnings": list(self._errors)[:20]}, deep=True)
            self._artifacts[artifact.artifact_id] = artifact
        self._artifact = artifact
        self._artifact_id = artifact.artifact_id
        self._process = None
        self._device_path = None
        self._segment_started_at = None
        self._directory = None
        self._state = RecordingState.READY
        self._stopping = False
        self._failure = None
        self._enforce_pressure(protected={artifact.artifact_id})
        return artifact

    async def stop(self, artifact_id: str | None = None) -> dict[str, object]:
        try:
            return await self._stop_impl(artifact_id)
        except asyncio.CancelledError:
            await self.aclose()
            raise

    async def _stop_impl(self, artifact_id: str | None = None) -> dict[str, object]:
        async with self._lock():
            self._prune_expired()
            if artifact_id is not None:
                if self._state != RecordingState.READY or self._artifact is None:
                    raise self._recording_error(
                        ErrorCode.RECORDING_NOT_ACTIVE,
                        "The requested recording is not in the ready state.",
                        "Wait for natural completion, then retrieve the ready artifact.",
                        data={"artifact_id": artifact_id, "state": self._state.value},
                    )
                if artifact_id != self._artifact.artifact_id:
                    raise self._recording_error(
                        ErrorCode.RECORDING_ARTIFACT_NOT_FOUND,
                        "The requested Android recording artifact is not the ready artifact.",
                        "Use the artifact_id returned by the current recording status.",
                        data={"artifact_id": artifact_id},
                    )
                return self._claim_artifact(artifact_id)
            if self._state == RecordingState.READY and self._artifact is not None:
                return self._claim_artifact(self._artifact.artifact_id)
            if not self.active or self._started_at is None:
                error = self._failure
                if error is None or self._state != RecordingState.FAILED:
                    error = self._recording_error(
                        ErrorCode.RECORDING_NOT_ACTIVE,
                        "No Android screen recording is active.",
                        "Call android_start_recording before stopping a recording.",
                        data={"state": self._state.value, "artifact_id": self._artifact_id},
                    )
                    self._failure = error
                raise error
            self._stopping = True
            self._state = RecordingState.STOPPING
            process = self._process
            if process is not None:
                try:
                    await _cleanup_process(
                        process,
                        timeout_seconds=self.config.subprocess_terminate_timeout_seconds,
                        reason="recording_stop",
                    )
                except Exception as error:
                    self._errors.append(f"Recording process cleanup failed: {type(error).__name__}")
            task = self._task
            if task is not None and task is not asyncio.current_task() and not task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=self.config.subprocess_timeout_seconds,
                    )
                except TimeoutError:
                    if process is not None:
                        with suppress(Exception):
                            process.kill()
                    task.cancel()
                    await _await_cancelled(task)
                    await self._pull_current_segment()
                    self._errors.append("Recording process required forced termination.")
            if self._device_path is not None:
                await self._pull_current_segment()
            if self._failure is not None:
                raise self._failure
            if self._artifact is None:
                await self._finish_after_segment()
            artifact = self._artifact
            if artifact is None:
                error = self._failure or self._recording_error(
                    ErrorCode.RECORDING_FAILED,
                    "No usable Android screen recording was captured.",
                    "Keep the device unlocked and retry a shorter recording.",
                )
                self._set_failed(error)
                raise error
            # Explicit stop is itself a retrieval/claim operation. The
            # completed artifact remains in the retention registry and can be
            # retrieved again by ID until retention evicts it.
            return self._claim_artifact(artifact.artifact_id)

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
        try:
            manifest.write_text(
                "".join(f"file '{path.name}'\n" for path in self._segments), encoding="utf-8"
            )
        except Exception as error:
            self._errors.append(
                f"Failed to write the recording merge manifest: {type(error).__name__}."
            )
            return self._segments[-1]
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
        except Exception as error:
            self._errors.append(f"ffmpeg could not merge segments: {type(error).__name__}.")
            return self._segments[-1]
        try:
            if result.ok and output.is_file() and output.stat().st_size > 0:
                return output
        except OSError:
            pass
        self._errors.append("ffmpeg could not merge segments; returning the last segment.")
        return self._segments[-1]

    def _claim_artifact(self, artifact_id: str) -> dict[str, object]:
        artifact = self._artifacts.get(artifact_id)
        if artifact is None:
            code = (
                ErrorCode.RECORDING_ARTIFACT_EXPIRED
                if artifact_id in self._expired_artifact_ids
                else ErrorCode.RECORDING_ARTIFACT_NOT_FOUND
            )
            raise self._recording_error(
                code,
                "The requested Android recording artifact is not available.",
                "List the current recording status and use a retained artifact_id.",
                data={"artifact_id": artifact_id},
            )
        if not self._artifact_path_exists(artifact_id):
            self._remove_artifact(artifact_id)
            raise self._recording_error(
                ErrorCode.RECORDING_ARTIFACT_NOT_FOUND,
                "The Android recording artifact is no longer present on the MCP host.",
                "Record a new video and retrieve it before local retention cleanup.",
                data={"artifact_id": artifact_id},
            )
        claimed = artifact.model_copy(update={"claimed": True}, deep=True)
        self._artifacts[artifact_id] = claimed
        try:
            self._persist_artifact(claimed)
        except OSError:
            # Retrieval remains valid even if metadata cannot be rewritten;
            # retain the in-memory claim and surface the durability issue in
            # the returned artifact warning.
            claimed = claimed.model_copy(
                update={
                    "warnings": [
                        *claimed.warnings,
                        "Failed to persist recording artifact metadata.",
                    ][:20]
                },
                deep=True,
            )
            self._artifacts[artifact_id] = claimed
        was_current = self._artifact_id == artifact_id and self._state == RecordingState.READY
        result = self._result(
            state=RecordingState.READY,
            artifact=claimed,
            retrieved=True,
        )
        if was_current:
            self._artifact = None
            self._artifact_id = None
            self._state = RecordingState.IDLE
            self._clear_active_fields(keep_artifact=False)
        return result

    async def retrieve(self, artifact_id: str) -> dict[str, object]:
        """Claim and return one retained artifact by opaque server ID."""

        if _ARTIFACT_ID_RE.fullmatch(artifact_id) is None:
            # No path is ever derived from caller input; the ID is only a
            # lookup key in the manager-owned registry.
            raise self._recording_error(
                ErrorCode.RECORDING_ARTIFACT_NOT_FOUND,
                "The recording artifact ID is invalid or not retained.",
                "Use the artifact_id returned by android_start_recording or status.",
                data={},
            )
        async with self._lock():
            self._prune_expired()
            return self._claim_artifact(artifact_id)

    retrieve_recording = retrieve

    def _artifact_path_exists(self, artifact_id: str) -> bool:
        artifact = self._artifacts.get(artifact_id)
        directory = self._artifact_directories.get(artifact_id)
        if artifact is None or directory is None:
            return False
        root = self._root()
        path = Path(artifact.recording_path)
        return self._under(directory, root) and self._under(path, directory) and path.is_file()

    def _remove_artifact(self, artifact_id: str) -> None:
        directory = self._artifact_directories.pop(artifact_id, None)
        self._artifact_expiry_monotonic.pop(artifact_id, None)
        self._artifacts.pop(artifact_id, None)
        if directory is not None:
            root = self._root()
            if directory.parent == root and self._under(directory, root):
                shutil.rmtree(directory, ignore_errors=True)
        if self._artifact_id == artifact_id and self._state == RecordingState.READY:
            self._artifact = None
            self._artifact_id = None
            self._state = RecordingState.IDLE

    def _prune_expired(self) -> list[str]:
        now = self._now()
        monotonic_now: float | None = None
        if self._artifact_expiry_monotonic:
            with suppress(Exception):
                monotonic_now = self.clock()
        expired = [
            artifact_id
            for artifact_id, artifact in self._artifacts.items()
            if artifact.expires_at <= now
            or (
                monotonic_now is not None
                and monotonic_now >= self._artifact_expiry_monotonic.get(artifact_id, float("inf"))
            )
        ]
        for artifact_id in sorted(expired):
            self._expired_artifact_ids.add(artifact_id)
            self._remove_artifact(artifact_id)
        return sorted(expired)

    def _enforce_pressure(self, *, protected: set[str] | None = None) -> list[str]:
        protected_ids = protected or set()
        evicted: list[str] = []
        while True:
            artifacts = list(self._artifacts.values())
            total_bytes = sum(item.size_bytes for item in artifacts)
            if len(artifacts) <= self.config.artifact_max_count and total_bytes <= (
                self.config.artifact_max_bytes
            ):
                break
            oversized = [
                item for item in artifacts if item.size_bytes > self.config.artifact_max_bytes
            ]
            if oversized:
                victim = min(oversized, key=lambda item: (item.completed_at, item.artifact_id))
                evicted.append(victim.artifact_id)
                self._remove_artifact(victim.artifact_id)
                continue
            candidates = [item for item in artifacts if item.artifact_id not in protected_ids]
            if not candidates:
                break
            victim = min(candidates, key=lambda item: (item.completed_at, item.artifact_id))
            evicted.append(victim.artifact_id)
            self._remove_artifact(victim.artifact_id)
        return evicted

    def prune_artifacts(self) -> list[str]:
        """Apply deterministic expiry and pressure retention policy."""

        expired = self._prune_expired()
        return expired + self._enforce_pressure()

    def status(self, operation_id: str | None = None) -> dict[str, object]:
        self._prune_expired()
        artifact = self._artifact if self._state == RecordingState.READY else None
        return self._result(
            state=self._state,
            operation_id=operation_id,
            artifact=artifact,
            success=True,
        )

    get_status = status

    async def _cleanup_incomplete(self, reason: str) -> None:
        process = self._process
        if process is not None:
            with suppress(Exception):
                await _cleanup_process(
                    process,
                    timeout_seconds=self.config.subprocess_terminate_timeout_seconds,
                    reason=reason,
                )
        task = self._task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await _await_cancelled(task)
        if self._device_path is not None:
            with suppress(Exception):
                await self._remove_device_file(self._device_path)
        directory = self._directory
        self._clear_active_fields(keep_artifact=False)
        if directory is not None:
            root = self._root()
            if directory.parent == root and self._under(directory, root):
                shutil.rmtree(directory, ignore_errors=True)

    async def _aclose_impl(self) -> None:
        """Run the one awaitable cleanup sequence used by disconnect/shutdown."""

        if not self.active and self._process is None and self._device_path is None:
            return
        self._stopping = True
        await self._cleanup_incomplete("owner_close")
        if self._state != RecordingState.READY:
            self._state = RecordingState.IDLE

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
        """Schedule the authoritative async owner cleanup for sync callers."""

        if self._state == RecordingState.READY and self._artifact is not None:
            return None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is not None and not self._loop.is_closed():
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
                self._device_path = None
            directory = self._directory
            self._clear_active_fields(keep_artifact=False)
            if directory is not None:
                root = self._root()
                if directory.parent == root and self._under(directory, root):
                    shutil.rmtree(directory, ignore_errors=True)
            self._state = RecordingState.IDLE
            return None
        if self._device_path is not None:
            with suppress(Exception):
                self.adb_device.shell(
                    ["rm", "-f", self._device_path],
                    timeout=self.config.subprocess_timeout_seconds,
                )
            self._device_path = None
        if self._directory is not None:
            directory = self._directory
            root = self._root()
            if directory.parent == root and self._under(directory, root):
                shutil.rmtree(directory, ignore_errors=True)
            self._directory = None
        return self._schedule_close()

    def _clear_active_fields(self, *, keep_artifact: bool) -> None:
        self._process = None
        self._task = None
        self._device_path = None
        self._started_at = None
        self._started_at_wall = None
        self._segment_started_at = None
        self._stopping = False
        self._directory = None
        self._segments = []
        self._segment_metadata = []
        self._max_duration_seconds = 0
        self._bit_rate = None
        if not keep_artifact:
            self._artifact = None
            self._artifact_id = None

    def _reset(self) -> None:
        """Compatibility reset that never deletes retained artifacts."""

        self._clear_active_fields(keep_artifact=False)
        self._state = RecordingState.IDLE
