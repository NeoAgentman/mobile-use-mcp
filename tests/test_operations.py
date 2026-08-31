import asyncio

import pytest

from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.operations import OperationGateway


@pytest.mark.asyncio
async def test_gateway_runs_operations_in_submission_order() -> None:
    gateway = OperationGateway(operation_timeout_seconds=1, queue_timeout_seconds=1)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first(_context: object) -> str:
        first_started.set()
        await release_first.wait()
        order.append("first")
        return "first"

    async def second(_context: object) -> str:
        order.append("second")
        return "second"

    first_task = asyncio.create_task(gateway.run("first", first))
    await first_started.wait()
    second_task = asyncio.create_task(gateway.run("second", second))
    await asyncio.sleep(0)
    assert order == []

    release_first.set()
    assert await first_task == "first"
    assert await second_task == "second"
    assert order == ["first", "second"]


@pytest.mark.asyncio
async def test_queue_wait_counts_against_total_budget_and_reports_stage() -> None:
    gateway = OperationGateway(operation_timeout_seconds=1, queue_timeout_seconds=0.02)
    active = asyncio.Event()
    release = asyncio.Event()

    async def blocker(_context: object) -> None:
        active.set()
        await release.wait()

    blocker_task = asyncio.create_task(gateway.run("blocker", blocker))
    await active.wait()
    with pytest.raises(MobileUseError) as caught:
        await gateway.run("queued", lambda _context: None, retry_safe=True, mutating=False)
    release.set()
    await blocker_task

    error = caught.value
    assert error.code == ErrorCode.TIMEOUT
    assert error.data["stage"] == "queue"
    assert error.data["retry_safe"] is True
    assert error.data["operation_id"]
    assert error.retryable is True


@pytest.mark.asyncio
async def test_execution_receives_only_remaining_total_budget_after_queue_wait() -> None:
    gateway = OperationGateway(operation_timeout_seconds=0.06, queue_timeout_seconds=1)
    active = asyncio.Event()
    release = asyncio.Event()

    async def blocker(_context: object) -> None:
        active.set()
        await release.wait()

    blocker_task = asyncio.create_task(gateway.run("blocker", blocker))
    await active.wait()
    queued = asyncio.create_task(
        gateway.run("queued", lambda _context: asyncio.sleep(1), retry_safe=True, mutating=False)
    )
    await asyncio.sleep(0.02)
    release.set()
    await blocker_task
    with pytest.raises(MobileUseError) as caught:
        await queued

    assert caught.value.code == ErrorCode.TIMEOUT
    assert caught.value.data["stage"] == "execution"
    assert caught.value.data["elapsed_ms"] >= 40


@pytest.mark.asyncio
async def test_execution_timeout_cancels_callback_and_prevents_commit() -> None:
    gateway = OperationGateway(operation_timeout_seconds=0.03, queue_timeout_seconds=1)
    committed: list[str] = []

    async def slow(context: object) -> None:
        await asyncio.sleep(1)
        # The callback must use the public context checkpoint before writing
        # session state; this should never be reached after the timeout.
        context.commit(lambda: committed.append("late"))  # type: ignore[attr-defined]

    with pytest.raises(MobileUseError) as caught:
        await gateway.run("slow", slow)

    assert caught.value.code == ErrorCode.TIMEOUT
    assert caught.value.data["stage"] == "execution"
    assert committed == []


@pytest.mark.asyncio
async def test_queued_cancellation_does_not_disturb_active_or_later_work() -> None:
    gateway = OperationGateway(operation_timeout_seconds=1, queue_timeout_seconds=1)
    active = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    async def blocker(_context: object) -> None:
        active.set()
        await release.wait()
        order.append("blocker")

    async def later(_context: object) -> None:
        order.append("later")

    blocker_task = asyncio.create_task(gateway.run("blocker", blocker))
    await active.wait()
    cancelled = asyncio.create_task(gateway.run("cancelled", lambda _context: order.append("bad")))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    release.set()
    await blocker_task
    await gateway.run("later", later)
    assert order == ["blocker", "later"]


@pytest.mark.asyncio
async def test_running_cancellation_invalidates_state_commit() -> None:
    gateway = OperationGateway(operation_timeout_seconds=1, queue_timeout_seconds=1)
    started = asyncio.Event()
    committed: list[str] = []

    async def cancellable(context: object) -> None:
        started.set()
        await asyncio.sleep(1)
        context.commit(lambda: committed.append("late"))  # type: ignore[attr-defined]

    task = asyncio.create_task(gateway.run("cancellable", cancellable))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await gateway.run("after-cancel", lambda _context: committed.append("after"))
    assert committed == ["after"]


@pytest.mark.asyncio
async def test_running_callback_keeps_fifo_owned_until_cancel_cleanup_finishes() -> None:
    gateway = OperationGateway(operation_timeout_seconds=1, queue_timeout_seconds=0.03)
    started = asyncio.Event()
    release = asyncio.Event()
    cleanup_finished = asyncio.Event()
    second_started = asyncio.Event()
    order: list[str] = []

    async def stubborn(_context: object) -> None:
        started.set()
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            # Simulate a third-party adapter that finishes its cleanup after
            # cancellation instead of propagating the cancellation.
            await release.wait()
            order.append("cancelled")
            cleanup_finished.set()

    async def after_timeout(_context: object) -> None:
        second_started.set()
        order.append("after")

    task = asyncio.create_task(gateway.run("stubborn", stubborn, timeout_seconds=0.02))
    await started.wait()
    with pytest.raises(MobileUseError) as caught:
        await task
    assert caught.value.code == ErrorCode.TIMEOUT

    try:
        with pytest.raises(MobileUseError) as queued:
            await gateway.run("after-timeout", after_timeout, timeout_seconds=0.05)
        assert queued.value.code == ErrorCode.TIMEOUT
        assert queued.value.data["stage"] == "queue"
        assert not second_started.is_set()
        assert order == []
    finally:
        release.set()

    await asyncio.wait_for(cleanup_finished.wait(), 0.2)
    await gateway.run("after-cleanup", after_timeout)
    assert order == ["cancelled", "after"]


@pytest.mark.asyncio
async def test_gateway_exposes_active_operation_until_callback_finishes() -> None:
    active_operation: list[str | None] = []
    gateway = OperationGateway(
        operation_timeout_seconds=1,
        queue_timeout_seconds=1,
        on_start=lambda context: active_operation.append(context.operation_id),
        on_finish=lambda _context: active_operation.append(None),
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def callback(_context: object) -> None:
        started.set()
        await release.wait()

    operation = asyncio.create_task(
        gateway.run("status-visible", callback, operation_id="op-visible")
    )
    await started.wait()
    assert active_operation == ["op-visible"]
    release.set()
    await operation
    assert active_operation == ["op-visible", None]
