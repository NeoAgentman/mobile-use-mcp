"""FIFO coordination for Android session operations.

The MCP server has one active Android session, so all work that can observe or
change that session goes through one queue.  The queue owns the lifetime of an
operation independently from the task waiting for its result: cancelling a
caller therefore cannot accidentally cancel the operation ahead of it or
release the queue while that operation is still being dispatched.

The gateway deliberately does not retry callbacks.  Read-only callers may
mark an operation as retry-safe for diagnostics, but retry policy remains with
the caller and no non-idempotent command is repeated after an uncertain result.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, TypeVar
from uuid import uuid4

from mobile_use_mcp.errors import ErrorCode, MobileUseError

T = TypeVar("T")
OperationCallback = Callable[["OperationContext"], T | Awaitable[T]]


_current_operation: ContextVar[OperationContext | None] = ContextVar(
    "mobile_use_current_operation", default=None
)


def current_operation() -> OperationContext | None:
    """Return the operation currently dispatching in this execution context."""

    return _current_operation.get()


@dataclass(slots=True)
class OperationContext:
    """Immutable identity plus cancellation/deadline state for one call.

    The cancellation flags can be changed by the task waiting on an
    operation while a synchronous Android adapter is running in a worker
    thread.  A small lock makes ``checkpoint`` and ``commit`` safe at that
    boundary; session state writers use ``commit`` before publishing results.
    """

    operation_id: str
    name: str
    submitted_at: float
    deadline: float
    retry_safe: bool
    mutating: bool
    _cancelled: bool = field(default=False, init=False, repr=False)
    _timed_out: bool = field(default=False, init=False, repr=False)
    _stage: str = field(default="queue", init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.submitted_at)

    @property
    def elapsed_ms(self) -> float:
        return round(self.elapsed_seconds * 1_000, 2)

    @property
    def stage(self) -> str:
        with self._lock:
            return self._stage

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def timed_out(self) -> bool:
        with self._lock:
            return self._timed_out

    @property
    def active(self) -> bool:
        with self._lock:
            return not self._cancelled and not self._timed_out and time.monotonic() < self.deadline

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def mark_running(self) -> None:
        with self._lock:
            self._stage = "execution"

    def mark_stage(self, stage: str) -> None:
        with self._lock:
            self._stage = stage

    def cancel(self, stage: str | None = None) -> None:
        with self._lock:
            self._cancelled = True
            if stage is not None:
                self._stage = stage

    def timeout(self, stage: str | None = None) -> None:
        with self._lock:
            self._timed_out = True
            if stage is not None:
                self._stage = stage

    def state_error(self, *, cancelled: bool) -> MobileUseError:
        stage = self.stage
        elapsed = self.elapsed_ms
        safe = self.retry_safe
        if cancelled:
            message = f"Android operation {self.name!r} was cancelled during {stage}."
            suggestion = (
                "Retry the read-only operation when the device is ready."
                if safe
                else "Do not blindly retry this operation; inspect session status first."
            )
            code = ErrorCode.CANCELLED
        else:
            message = f"Android operation {self.name!r} timed out during {stage}."
            suggestion = (
                "Retry this bounded read-only operation if the device is responsive."
                if safe
                else (
                    "The command may have reached the device; observe or inspect status "
                    "before retrying."
                )
            )
            code = ErrorCode.TIMEOUT
        return MobileUseError(
            code,
            message,
            suggestion,
            data={
                "operation_id": self.operation_id,
                "stage": stage,
                "elapsed_ms": elapsed,
                "retry_safe": safe,
                "mutating": self.mutating,
            },
            retryable_override=safe,
        )

    def checkpoint(self, stage: str = "execution") -> None:
        """Reject a callback that can no longer safely publish state."""

        self.mark_stage(stage)
        with self._lock:
            cancelled = self._cancelled
            timed_out = self._timed_out
            expired = time.monotonic() >= self.deadline
        if cancelled:
            raise self.state_error(cancelled=True)
        if timed_out or expired:
            if expired and not timed_out:
                self.timeout(stage)
            raise self.state_error(cancelled=False)

    def commit(self, callback: Callable[[], T], stage: str = "commit") -> T:
        """Run one state publication only while this operation is active."""

        self.mark_stage(stage)
        with self._lock:
            if self._cancelled:
                raise self.state_error(cancelled=True)
            if self._timed_out or time.monotonic() >= self.deadline:
                self._timed_out = True
                raise self.state_error(cancelled=False)
            return callback()

    async def sleep(self, delay_seconds: float) -> None:
        """Sleep with cancellation/deadline checks for fixed-delay waits."""

        self.checkpoint("execution")
        await asyncio.sleep(min(max(0.0, delay_seconds), self.remaining_seconds()))
        self.checkpoint("execution")


@dataclass(slots=True)
class _QueuedOperation[T]:
    context: OperationContext
    callback: OperationCallback[T]
    future: asyncio.Future[T]
    started: asyncio.Event
    abort_signal: asyncio.Event
    task: asyncio.Task[Any] | None = None
    running: bool = False
    cancelled: bool = False


class OperationGateway:
    """A single-consumer FIFO queue with total operation deadlines."""

    def __init__(
        self,
        *,
        operation_timeout_seconds: float,
        queue_timeout_seconds: float,
        on_start: Callable[[OperationContext], None] | None = None,
        on_finish: Callable[[OperationContext], None] | None = None,
    ) -> None:
        self.operation_timeout_seconds = operation_timeout_seconds
        self.queue_timeout_seconds = queue_timeout_seconds
        self._on_start = on_start
        self._on_finish = on_finish
        self._queue: deque[_QueuedOperation[Any]] = deque()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._worker_task: asyncio.Task[None] | None = None

    def _bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is loop:
            return
        if self._worker_task is not None and not self._worker_task.done():
            raise RuntimeError("DeviceSession operation gateway cannot span event loops while busy")
        self._loop = loop
        self._wake = asyncio.Event()
        self._worker_task = None

    def _ensure_worker(self) -> None:
        assert self._wake is not None
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._worker(), name="android-operation-gateway"
            )

    @staticmethod
    def _set_future_result(item: _QueuedOperation[Any], value: Any) -> None:
        if not item.future.done():
            item.future.set_result(value)

    @staticmethod
    def _set_future_error(item: _QueuedOperation[Any], error: BaseException) -> None:
        if not item.future.done():
            item.future.set_exception(error)
            # A caller may have been cancelled or timed out and therefore no
            # longer await this future.  Mark the exception as observed while
            # retaining it for a race-winning waiter that does await it.
            item.future.exception()

    @staticmethod
    def _annotate_error(item: _QueuedOperation[Any], error: MobileUseError) -> MobileUseError:
        """Attach bounded gateway context without exposing callback internals."""

        details = dict(error.data)
        details.setdefault("operation_id", item.context.operation_id)
        details.setdefault("stage", item.context.stage)
        details.setdefault("retry_safe", item.context.retry_safe)
        return MobileUseError(
            error.code,
            error.message,
            error.suggestion,
            data=details,
            retryable_override=error.retryable,
        )

    def _remove_queued(self, item: _QueuedOperation[Any]) -> None:
        with suppress(ValueError):
            self._queue.remove(item)

    @staticmethod
    def _consume_task_exception(task: asyncio.Task[Any]) -> None:
        """Observe a detached callback exception after caller cancellation."""

        if not task.cancelled():
            with suppress(BaseException):
                task.exception()

    async def _invoke(self, item: _QueuedOperation[Any]) -> Any:
        token: Token[OperationContext | None] = _current_operation.set(item.context)
        try:
            callback_result = item.callback(item.context)
            if inspect.isawaitable(callback_result):
                return await callback_result
            return callback_result
        finally:
            _current_operation.reset(token)

    async def _worker(self) -> None:
        assert self._wake is not None
        while True:
            await self._wake.wait()
            while self._queue:
                item = self._queue.popleft()
                if item.cancelled or item.future.done():
                    continue
                item.running = True
                item.context.mark_running()
                item.started.set()
                if self._on_start is not None:
                    self._on_start(item.context)
                try:
                    if item.context.remaining_seconds() <= 0:
                        item.context.timeout("execution")
                        self._set_future_error(item, item.context.state_error(cancelled=False))
                        continue
                    item.task = asyncio.create_task(
                        self._invoke(item), name=f"android-operation-{item.context.operation_id}"
                    )
                    item.task.add_done_callback(self._consume_task_exception)
                    abort_wait = asyncio.create_task(item.abort_signal.wait())
                    try:
                        done, _ = await asyncio.wait(
                            {item.task, abort_wait},
                            timeout=item.context.remaining_seconds(),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if not done:
                            item.context.timeout("execution")
                            item.task.cancel()
                            self._set_future_error(item, item.context.state_error(cancelled=False))
                        elif abort_wait in done:
                            item.task.cancel()
                            self._set_future_error(
                                item,
                                item.context.state_error(cancelled=item.context.cancelled),
                            )
                        else:
                            try:
                                value = item.task.result()
                            except asyncio.CancelledError:
                                item.context.cancel("execution")
                                self._set_future_error(
                                    item,
                                    item.context.state_error(cancelled=True),
                                )
                            except MobileUseError as error:
                                self._set_future_error(item, self._annotate_error(item, error))
                            except Exception as error:
                                self._set_future_error(item, error)
                            else:
                                if item.context.active:
                                    self._set_future_result(item, value)
                                else:
                                    self._set_future_error(
                                        item,
                                        item.context.state_error(
                                            cancelled=item.context.cancelled,
                                        ),
                                    )
                    finally:
                        abort_wait.cancel()
                finally:
                    item.running = False
                    if self._on_finish is not None:
                        self._on_finish(item.context)
            self._wake.clear()

    async def _cancel_item(self, item: _QueuedOperation[Any]) -> None:
        item.cancelled = True
        item.context.cancel("queue" if not item.running else "execution")
        if not item.running:
            self._remove_queued(item)
        elif item.task is not None and not item.task.done():
            item.abort_signal.set()
            item.task.cancel()
        elif item.running:
            item.abort_signal.set()
        self._set_future_error(item, item.context.state_error(cancelled=True))

    async def _timeout_item(self, item: _QueuedOperation[Any], stage: str) -> MobileUseError:
        if item.running:
            item.context.timeout("execution")
            item.abort_signal.set()
            if item.task is not None and not item.task.done():
                item.task.cancel()
        else:
            item.cancelled = True
            item.context.timeout(stage)
            self._remove_queued(item)
        error = item.context.state_error(cancelled=False)
        self._set_future_error(item, error)
        return error

    async def run(
        self,
        name: str,
        callback: OperationCallback[T],
        *,
        operation_id: str | None = None,
        timeout_seconds: float | None = None,
        retry_safe: bool = False,
        mutating: bool = True,
    ) -> T:
        """Queue and await one operation under a total monotonic deadline."""

        loop = asyncio.get_running_loop()
        self._bind_loop(loop)
        configured_budget = self.operation_timeout_seconds
        requested_budget = configured_budget if timeout_seconds is None else timeout_seconds
        budget = min(configured_budget, requested_budget)
        if budget <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        now = time.monotonic()
        context = OperationContext(
            operation_id=operation_id or f"op-{uuid4().hex}",
            name=name,
            submitted_at=now,
            deadline=now + budget,
            retry_safe=retry_safe,
            mutating=mutating,
        )
        future: asyncio.Future[T] = loop.create_future()
        item = _QueuedOperation(
            context=context,
            callback=callback,
            future=future,
            started=asyncio.Event(),
            abort_signal=asyncio.Event(),
        )
        self._queue.append(item)
        assert self._wake is not None
        self._wake.set()
        self._ensure_worker()

        queue_budget = min(
            context.remaining_seconds(),
            max(0.0, self.queue_timeout_seconds),
        )
        try:
            await asyncio.wait_for(asyncio.shield(item.started.wait()), queue_budget)
        except TimeoutError:
            if item.running or item.started.is_set():
                # The worker won the race to dispatch.  Its remaining total
                # budget is now the execution budget.
                pass
            else:
                error = await self._timeout_item(item, "queue")
                raise error from None
        except asyncio.CancelledError:
            await self._cancel_item(item)
            raise

        if not item.started.is_set():
            # A queue timeout may have happened immediately after dispatch
            # was observed.  The worker will finish the item, but this caller
            # must receive a typed timeout rather than wait indefinitely.
            raise await self._timeout_item(item, "queue")
        remaining = context.remaining_seconds()
        if remaining <= 0:
            error = await self._timeout_item(item, "execution")
            raise error from None
        try:
            return await asyncio.wait_for(asyncio.shield(future), remaining)
        except TimeoutError:
            if future.done():
                return await future
            error = await self._timeout_item(item, "execution")
            raise error from None
        except asyncio.CancelledError:
            await self._cancel_item(item)
            raise

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        """Compatibility alias for callers that call the gateway ``execute``."""

        return await self.run(*args, **kwargs)


__all__ = [
    "OperationCallback",
    "OperationContext",
    "OperationGateway",
    "current_operation",
]
