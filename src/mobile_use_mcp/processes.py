"""Owned, bounded execution of local ADB and media helper processes.

The server talks to a device through two different libraries, but the local
``adb`` and ``ffmpeg`` children have the same ownership rules.  This module is
the single boundary for those children.  It deliberately accepts an argv
sequence only; callers cannot accidentally turn a device value into shell
syntax.

``ProcessRunner`` has both an async API (used by recording and cancellable MCP
operations) and a small synchronous compatibility API (used by the legacy
device registry).  Both APIs apply the same validation, deadline, output
budget, and terminate/kill/reap policy.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast

_ORIGINAL_SUBPROCESS_RUN = subprocess.run


class ProcessError(RuntimeError):
    """Base class for safe process-boundary failures."""


class ProcessStartError(ProcessError):
    """The executable could not be started."""


class ProcessTimeoutError(ProcessError):
    """A child exceeded its monotonic deadline."""


class ProcessCancelledError(asyncio.CancelledError):
    """A child was cleaned up because its owner was cancelled."""


class ProcessOutputLimitError(ProcessError):
    """A child produced more output than the configured capture budget."""

    def __init__(self, message: str, *, stream: str, limit: int) -> None:
        super().__init__(message)
        self.stream = stream
        self.limit = limit


async def run_blocking[T](
    function: Callable[..., T],
    *args: Any,
    timeout_seconds: float,
    on_abort: Callable[[], None | Awaitable[None]] | None = None,
    **kwargs: Any,
) -> T:
    """Run a blocking device-library call with a finite owner boundary.

    Cancelling an ``asyncio.to_thread`` await does not stop its non-daemon
    worker.  This helper uses a daemon worker, invokes the caller's
    adapter-close hook, and observes the eventual exception/result so it
    cannot become an unhandled task.  Production adapters pass their socket
    close hook, making the library-level timeout interruptible as well as
    bounded; a broken third-party call cannot hold interpreter shutdown open.
    """

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    loop = asyncio.get_running_loop()
    worker: asyncio.Future[T] = loop.create_future()

    def invoke() -> None:
        try:
            value = function(*args, **kwargs)
        except BaseException as error:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(_set_future_error, worker, error)
        else:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(_set_future_result, worker, value)

    thread = threading.Thread(target=invoke, name="mobile-use-blocking-call", daemon=True)
    thread.start()

    async def abort_adapter() -> None:
        if on_abort is None:
            return
        try:
            result = on_abort()
            if isinstance(result, Awaitable):
                # Adapter close is best effort and must not turn an already
                # classified timeout/cancellation into an unbounded wait.
                await asyncio.wait_for(result, min(timeout_seconds, 1.0))
        except Exception:
            pass

    try:
        return await asyncio.wait_for(asyncio.shield(worker), timeout_seconds)
    except TimeoutError as error:
        await abort_adapter()
        worker.add_done_callback(_consume_future_exception)
        raise ProcessTimeoutError("Blocking device-library call exceeded its deadline.") from error
    except asyncio.CancelledError:
        await abort_adapter()
        worker.add_done_callback(_consume_future_exception)
        raise


def validate_argv(argv: Any) -> tuple[str, ...]:
    """Validate and freeze an executable argv sequence.

    A string is intentionally rejected even though ``subprocess`` accepts it
    in a few APIs.  Empty arguments are useful to neither ADB nor ffmpeg and
    NUL bytes are rejected by every supported operating system.
    """

    if isinstance(argv, (str, bytes, bytearray)):
        raise TypeError("argv must be a sequence of strings, not a shell command string")
    values = tuple(argv)
    if not values:
        raise ValueError("argv must contain an executable")
    if any(not isinstance(value, str) for value in values):
        raise TypeError("argv entries must be strings")
    if any(value == "" for value in values):
        raise ValueError("argv entries must not be empty")
    if any("\x00" in value for value in values):
        raise ValueError("argv entries must not contain NUL bytes")
    return values


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Bounded result of one completed child process."""

    argv: tuple[str, ...]
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True, slots=True)
class CleanupReport:
    """What the bounded cleanup path had to do for a child."""

    reason: str
    terminated: bool
    killed: bool
    reaped: bool
    returncode: int | None


async def _read_limited(
    stream: asyncio.StreamReader,
    *,
    name: str,
    limit: int,
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await stream.read(min(64 * 1024, max(1, limit - size + 1)))
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise ProcessOutputLimitError(
                f"{name} output exceeded the configured capture budget.",
                stream=name,
                limit=limit,
            )
        chunks.append(chunk)


def _set_future_result(future: asyncio.Future[Any], value: Any) -> None:
    if not future.done():
        future.set_result(value)


def _set_future_error(future: asyncio.Future[Any], error: BaseException) -> None:
    if not future.done():
        future.set_exception(error)
        # The owner may have timed out and detached already.  Observe the
        # exception now so Python does not report an unhandled Future later.
        future.exception()


def _consume_future_exception(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        with suppress(BaseException):
            future.exception()


async def _gather_process_tasks(
    wait_task: asyncio.Task[int],
    stdout_task: asyncio.Task[bytes],
    stderr_task: asyncio.Task[bytes],
) -> tuple[int, bytes, bytes]:
    result = await asyncio.gather(wait_task, stdout_task, stderr_task)
    return result[0], result[1], result[2]


class OwnedProcess:
    """An asyncio child process whose lifetime is owned by one runner.

    ``cleanup`` is idempotent and is the only method that escalates signals or
    waits for reaping.  ``terminate`` and ``kill`` remain synchronous signal
    helpers for compatibility with the recording process protocol.
    """

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        argv: tuple[str, ...],
        started_at: float,
        terminate_timeout_seconds: float,
        process_group: bool,
        capture_output: bool,
        max_output_bytes: int,
    ) -> None:
        self._process = process
        self.argv = argv
        self.started_at = started_at
        self._terminate_timeout_seconds = terminate_timeout_seconds
        self._process_group = process_group
        self._capture_output = capture_output
        self._max_output_bytes = max_output_bytes
        self._wait_task: asyncio.Task[int] | None = None
        self._stdout_task: asyncio.Task[bytes] | None = None
        self._stderr_task: asyncio.Task[bytes] | None = None
        self._cleanup_task: asyncio.Task[CleanupReport] | None = None
        self._cleanup_lock = asyncio.Lock()
        self._cleaned = False

    @property
    def pid(self) -> int | None:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def _wait_task_for(self) -> asyncio.Task[int]:
        if self._wait_task is None:
            self._wait_task = asyncio.create_task(
                self._process.wait(), name="mobile-use-process-wait"
            )
        return self._wait_task

    async def wait(self) -> int:
        """Wait for a process and reap it.

        Recording children discard their output at spawn time, so this method
        cannot deadlock on a full pipe.  Capturing callers should use
        ``communicate`` so both streams are drained under the output budget.
        """

        wait_task = self._wait_task_for()
        stdout_task, stderr_task = self._capture_tasks()
        try:
            returncode, _, _ = await asyncio.gather(wait_task, stdout_task, stderr_task)
        except ProcessOutputLimitError:
            await self.cleanup(reason="output_limit")
            raise
        except asyncio.CancelledError:
            await self.cleanup(reason="cancelled")
            raise
        except BaseException:
            # A signal or unexpected reader failure must not bypass child
            # cleanup.  In particular, SIGTERM is deliberately surfaced as a
            # Python interruption by the stdio entry point.
            await self.cleanup(reason="owner_error")
            raise
        finally:
            for task in (stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
                await _gather_cancelled(task)
        return returncode

    def _capture_tasks(self) -> tuple[asyncio.Task[bytes], asyncio.Task[bytes]]:
        if self._stdout_task is not None and self._stderr_task is not None:
            return self._stdout_task, self._stderr_task
        if not self._capture_output:
            self._stdout_task = asyncio.create_task(asyncio.sleep(0, result=b""))
            self._stderr_task = asyncio.create_task(asyncio.sleep(0, result=b""))
        else:
            stdout = self._process.stdout
            stderr = self._process.stderr
            assert stdout is not None and stderr is not None
            self._stdout_task = asyncio.create_task(
                _read_limited(stdout, name="stdout", limit=self._max_output_bytes),
                name="mobile-use-process-stdout",
            )
            self._stderr_task = asyncio.create_task(
                _read_limited(stderr, name="stderr", limit=self._max_output_bytes),
                name="mobile-use-process-stderr",
            )
        return self._stdout_task, self._stderr_task

    def _send_signal(self, kind: str) -> None:
        if self.returncode is not None:
            return
        pid = self.pid
        if self._process_group and pid is not None and os.name == "posix":
            sig = signal.SIGTERM if kind == "terminate" else signal.SIGKILL
            try:
                os.killpg(pid, sig)
                return
            except (ProcessLookupError, PermissionError, OSError):
                # Fall back to the child handle.  This also keeps lightweight
                # test doubles and unusual process implementations usable.
                pass
        if kind == "terminate":
            self._process.terminate()
        else:
            self._process.kill()

    def terminate(self) -> None:
        self._send_signal("terminate")

    def kill(self) -> None:
        self._send_signal("kill")

    async def _wait_bounded(self, timeout_seconds: float) -> tuple[bool, int | None]:
        wait_task = self._wait_task_for()
        try:
            result = await asyncio.wait_for(asyncio.shield(wait_task), timeout_seconds)
        except TimeoutError:
            return False, self.returncode
        return True, result

    async def _cleanup_once(self, reason: str) -> CleanupReport:
        async with self._cleanup_lock:
            if self._cleaned:
                return CleanupReport(
                    reason=reason,
                    terminated=False,
                    killed=False,
                    reaped=self.returncode is not None,
                    returncode=self.returncode,
                )
            self._cleaned = True
            terminated = False
            killed = False
            reaped = self.returncode is not None
            if not reaped:
                with suppress(Exception):
                    self.terminate()
                terminated = True
                reaped, _ = await self._wait_bounded(self._terminate_timeout_seconds)
            if not reaped:
                with suppress(Exception):
                    self.kill()
                killed = True
                reaped, _ = await self._wait_bounded(self._terminate_timeout_seconds)
            if not reaped:
                # A successful kill should make this wait complete on normal
                # OSes.  Keep a detached reaper for a hostile test double or
                # unusual platform so cleanup itself remains bounded while a
                # real child is still eventually collected.
                reaper = self._wait_task_for()
                reaper.add_done_callback(_consume_task_exception)
            self._close_pipes()
            return CleanupReport(
                reason=reason,
                terminated=terminated,
                killed=killed,
                reaped=reaped,
                returncode=self.returncode,
            )

    def _close_pipes(self) -> None:
        """Close asyncio pipe transports after the child has been reaped.

        ``StreamReader`` intentionally has no public ``close`` method.  Its
        private transport is nevertheless the owned resource created by
        ``create_subprocess_exec``; closing it here prevents late destructor
        callbacks from trying to schedule work on a closed event loop.
        """

        for stream in (self._process.stdout, self._process.stderr):
            transport = getattr(stream, "_transport", None)
            close = getattr(transport, "close", None)
            if callable(close):
                close()

    async def cleanup(self, *, reason: str = "owner_close") -> CleanupReport:
        """Terminate, bounded-wait, kill if necessary, and reap this child."""

        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(
                self._cleanup_once(reason), name="mobile-use-process-cleanup"
            )
        return await asyncio.shield(self._cleanup_task)

    async def communicate(
        self,
        *,
        timeout_seconds: float | None = None,
        deadline: float | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> ProcessResult:
        """Drain bounded stdout/stderr while observing a monotonic deadline."""

        started = self.started_at
        stdout_task, stderr_task = self._capture_tasks()
        wait_task = self._wait_task_for()
        all_task = asyncio.create_task(
            _gather_process_tasks(wait_task, stdout_task, stderr_task),
            name="mobile-use-process-communicate",
        )
        cancel_task: asyncio.Task[bool] | None = None
        if cancel_event is not None:
            cancel_task = asyncio.create_task(cancel_event.wait(), name="mobile-use-process-cancel")
        try:
            remaining = _remaining_seconds(timeout_seconds=timeout_seconds, deadline=deadline)
            pending = {all_task}
            if cancel_task is not None:
                pending.add(cancel_task)  # type: ignore[arg-type]
            done, _ = await asyncio.wait(
                pending,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                await self.cleanup(reason="timeout")
                raise ProcessTimeoutError(f"Process exceeded its {remaining:.3f}s deadline.")
            if cancel_task is not None and cancel_task in done:
                await self.cleanup(reason="cancelled")
                raise ProcessCancelledError()
            try:
                wait_result, stdout_result, stderr_result = await all_task
            except ProcessOutputLimitError:
                await self.cleanup(reason="output_limit")
                raise
            return ProcessResult(
                argv=self.argv,
                returncode=wait_result,
                stdout=stdout_result,
                stderr=stderr_result,
                started_at=started,
                finished_at=time.monotonic(),
            )
        except asyncio.CancelledError:
            await self.cleanup(reason="cancelled")
            raise
        except BaseException:
            await self.cleanup(reason="owner_error")
            raise
        finally:
            if cancel_task is not None:
                cancel_task.cancel()
                await _gather_cancelled(cancel_task)
            if not all_task.done():
                all_task.cancel()
                await _gather_cancelled(all_task)
            for task in (stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
                await _gather_cancelled(task)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.cleanup(reason="context_exit")


def _remaining_seconds(*, timeout_seconds: float | None, deadline: float | None) -> float | None:
    values: list[float] = []
    if timeout_seconds is not None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        values.append(timeout_seconds)
    if deadline is not None:
        values.append(deadline - time.monotonic())
    if not values:
        return None
    return max(0.0, min(values))


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        with suppress(BaseException):
            task.exception()


async def _gather_cancelled(task: asyncio.Task[Any]) -> None:
    with suppress(BaseException):
        await task


class ProcessRunner:
    """Create and execute owned local processes under one policy."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        max_output_bytes: int = 1 * 1024 * 1024,
        terminate_timeout_seconds: float = 2.0,
        process_group: bool = True,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be greater than zero")
        if terminate_timeout_seconds <= 0:
            raise ValueError("terminate_timeout_seconds must be greater than zero")
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.terminate_timeout_seconds = terminate_timeout_seconds
        self.process_group = process_group

    @classmethod
    def from_config(cls, config: Any) -> Self:
        return cls(
            timeout_seconds=config.subprocess_timeout_seconds,
            max_output_bytes=config.subprocess_max_output_bytes,
            terminate_timeout_seconds=config.subprocess_terminate_timeout_seconds,
        )

    async def start(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        capture_output: bool = True,
    ) -> OwnedProcess:
        values = validate_argv(argv)
        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL,
            "stderr": asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL,
            "cwd": str(cwd) if cwd is not None else None,
            "env": dict(env) if env is not None else None,
        }
        if os.name == "posix" and self.process_group:
            kwargs["start_new_session"] = True
        elif os.name == "nt" and self.process_group:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        started = time.monotonic()
        spawn_task = asyncio.create_task(
            asyncio.create_subprocess_exec(*values, **kwargs),
            name="mobile-use-process-start",
        )
        try:
            process = await asyncio.shield(spawn_task)
        except asyncio.CancelledError:
            # Cancellation can arrive in the small window after the OS has
            # accepted the spawn request.  Finish observing that request,
            # then hand any resulting child to the same bounded owner-close
            # path before propagating cancellation to the caller.
            try:
                process = await asyncio.shield(spawn_task)
            except BaseException:
                spawn_task.add_done_callback(_consume_task_exception)
                raise
            child = OwnedProcess(
                process,
                argv=values,
                started_at=started,
                terminate_timeout_seconds=self.terminate_timeout_seconds,
                process_group=self.process_group,
                capture_output=capture_output,
                max_output_bytes=self.max_output_bytes,
            )
            with suppress(BaseException):
                await asyncio.shield(child.cleanup(reason="start_cancelled"))
            raise
        except (FileNotFoundError, PermissionError, OSError) as error:
            raise ProcessStartError("The configured process could not be started.") from error
        return OwnedProcess(
            process,
            argv=values,
            started_at=started,
            terminate_timeout_seconds=self.terminate_timeout_seconds,
            process_group=self.process_group,
            capture_output=capture_output,
            max_output_bytes=self.max_output_bytes,
        )

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float | None = None,
        deadline: float | None = None,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> ProcessResult:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if deadline is not None and deadline <= time.monotonic():
            raise ProcessTimeoutError("Process deadline elapsed before it could start.")
        budget = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        budget = min(self.timeout_seconds, budget)
        child = await self.start(argv, cwd=cwd, env=env)
        return await child.communicate(
            timeout_seconds=budget,
            deadline=deadline,
            cancel_event=cancel_event,
        )

    async def execute(self, *args: Any, **kwargs: Any) -> ProcessResult:
        """Compatibility alias for callers that call the runner ``execute``."""

        return await self.run(*args, **kwargs)

    def run_sync(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float | None = None,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        """Synchronous compatibility path with the same bounded result shape.

        Device discovery is called from a worker thread by the session
        gateway.  ``subprocess.run`` already terminates and waits for a child
        on timeout; the async path above is used whenever caller cancellation
        needs to reach a running child immediately.
        """

        values = validate_argv(argv)
        budget = (
            self.timeout_seconds
            if timeout_seconds is None
            else min(self.timeout_seconds, timeout_seconds)
        )
        if budget <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        started = time.monotonic()
        # A few older embedders patch ``subprocess.run`` at the devices module
        # seam.  Keep that explicit testing/integration seam source-compatible
        # while production takes the owned Popen path below.
        if subprocess.run is not _ORIGINAL_SUBPROCESS_RUN:
            return self._run_sync_compat(values, budget, started, cwd=cwd, env=env)
        process: subprocess.Popen[bytes] | None = None
        try:
            kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "shell": False,
                "text": False,
                "cwd": str(cwd) if cwd is not None else None,
                "env": dict(env) if env is not None else None,
            }
            if os.name == "posix" and self.process_group:
                kwargs["start_new_session"] = True
            elif os.name == "nt" and self.process_group:
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            process = cast(subprocess.Popen[bytes], subprocess.Popen(list(values), **kwargs))
        except (FileNotFoundError, PermissionError, OSError) as error:
            raise ProcessStartError("The configured process could not be started.") from error
        assert process is not None
        try:
            stdout, stderr = self._capture_sync(process, budget)
        except BaseException:
            self._cleanup_sync(process)
            raise
        assert process.returncode is not None
        if len(stdout) > self.max_output_bytes:
            raise ProcessOutputLimitError(
                "stdout output exceeded the configured capture budget.",
                stream="stdout",
                limit=self.max_output_bytes,
            )
        if len(stderr) > self.max_output_bytes:
            raise ProcessOutputLimitError(
                "stderr output exceeded the configured capture budget.",
                stream="stderr",
                limit=self.max_output_bytes,
            )
        return ProcessResult(
            argv=values,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            started_at=started,
            finished_at=time.monotonic(),
        )

    def _capture_sync(
        self,
        process: subprocess.Popen[bytes],
        budget: float,
    ) -> tuple[bytes, bytes]:
        """Drain both pipes incrementally without retaining over-budget data."""

        stdout = process.stdout
        stderr = process.stderr
        assert stdout is not None and stderr is not None
        buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        overflow: list[str] = []

        def consume(stream: Any, name: str) -> None:
            try:
                while chunk := stream.read(64 * 1024):
                    current = buffers[name]
                    remaining = max(0, self.max_output_bytes - len(current))
                    if len(chunk) > remaining and name not in overflow:
                        overflow.append(name)
                    if remaining:
                        current.extend(chunk[:remaining])
            except (OSError, ValueError):
                # Process cleanup closes the pipe; the result is already
                # classified by timeout/output overflow where applicable.
                pass
            finally:
                with suppress(Exception):
                    stream.close()

        readers = [
            threading.Thread(
                target=consume,
                args=(stdout, "stdout"),
                name="mobile-use-sync-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=consume,
                args=(stderr, "stderr"),
                name="mobile-use-sync-stderr",
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + budget
        timed_out = False
        while process.poll() is None:
            if overflow:
                self._cleanup_sync(process)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                self._cleanup_sync(process)
                break
            time.sleep(min(0.01, remaining))
        if timed_out:
            for reader in readers:
                reader.join(timeout=self.terminate_timeout_seconds)
            raise ProcessTimeoutError("Process exceeded its synchronous deadline.")
        for reader in readers:
            reader.join(timeout=self.terminate_timeout_seconds)
        if overflow:
            raise ProcessOutputLimitError(
                f"{overflow[0]} output exceeded the configured capture budget.",
                stream=overflow[0],
                limit=self.max_output_bytes,
            )
        return bytes(buffers["stdout"]), bytes(buffers["stderr"])

    def _run_sync_compat(
        self,
        values: tuple[str, ...],
        budget: float,
        started: float,
        *,
        cwd: str | Path | None,
        env: Mapping[str, str] | None,
    ) -> ProcessResult:
        """Call a deliberately replaced ``subprocess.run`` test seam."""

        try:
            if cwd is None and env is None:
                result = cast(
                    subprocess.CompletedProcess[Any],
                    subprocess.run(
                        list(values),
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=budget,
                    ),
                )
            else:
                result = cast(
                    subprocess.CompletedProcess[Any],
                    subprocess.run(
                        list(values),
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=budget,
                        cwd=str(cwd) if cwd is not None else None,
                        env=dict(env) if env is not None else None,
                    ),
                )
        except subprocess.TimeoutExpired as error:
            raise ProcessTimeoutError("Process exceeded its synchronous deadline.") from error
        stdout = (
            b""
            if result.stdout is None
            else result.stdout
            if isinstance(result.stdout, bytes)
            else str(result.stdout).encode()
        )
        stderr = (
            b""
            if result.stderr is None
            else result.stderr
            if isinstance(result.stderr, bytes)
            else str(result.stderr).encode()
        )
        if len(stdout) > self.max_output_bytes:
            raise ProcessOutputLimitError(
                "stdout output exceeded the configured capture budget.",
                stream="stdout",
                limit=self.max_output_bytes,
            )
        if len(stderr) > self.max_output_bytes:
            raise ProcessOutputLimitError(
                "stderr output exceeded the configured capture budget.",
                stream="stderr",
                limit=self.max_output_bytes,
            )
        return ProcessResult(
            argv=values,
            returncode=result.returncode,
            stdout=stdout,
            stderr=stderr,
            started_at=started,
            finished_at=time.monotonic(),
        )

    def _cleanup_sync(self, process: subprocess.Popen[bytes]) -> None:
        """Apply terminate -> wait -> kill -> final reap synchronously."""

        if process.poll() is not None:
            return
        self._send_sync_signal(process, signal.SIGTERM)
        try:
            process.wait(timeout=self.terminate_timeout_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        self._send_sync_signal(process, signal.SIGKILL)
        try:
            process.wait(timeout=self.terminate_timeout_seconds)
        except subprocess.TimeoutExpired:
            # Keep the synchronous request bounded even for a hostile process
            # wrapper; a daemon reaper prevents a zombie if it exits later.
            threading.Thread(
                target=process.wait,
                name="mobile-use-sync-reaper",
                daemon=True,
            ).start()

    def _send_sync_signal(self, process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        if os.name == "posix" and self.process_group:
            with suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(process.pid, sig)
                return
        with suppress(OSError, ValueError):
            if sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()


__all__ = [
    "CleanupReport",
    "OwnedProcess",
    "ProcessCancelledError",
    "ProcessError",
    "ProcessOutputLimitError",
    "ProcessResult",
    "ProcessRunner",
    "ProcessStartError",
    "ProcessTimeoutError",
    "run_blocking",
    "validate_argv",
]
