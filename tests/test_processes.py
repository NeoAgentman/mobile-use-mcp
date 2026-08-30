import asyncio
import signal
import sys
import time

import pytest

from mobile_use_mcp.processes import (
    ProcessOutputLimitError,
    ProcessRunner,
    ProcessTimeoutError,
    run_blocking,
    validate_argv,
)


def test_validate_argv_rejects_shell_strings_and_nul() -> None:
    with pytest.raises(TypeError):
        validate_argv("python -c pass")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        validate_argv([sys.executable, "-c", "bad\x00value"])


@pytest.mark.asyncio
async def test_runner_captures_both_streams_with_a_bounded_result() -> None:
    runner = ProcessRunner(timeout_seconds=2, max_output_bytes=128, terminate_timeout_seconds=0.1)
    result = await runner.run(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr)",
        ]
    )

    assert result.ok
    assert result.stdout_text.strip() == "out"
    assert result.stderr_text.strip() == "err"
    assert result.duration_seconds >= 0


@pytest.mark.asyncio
async def test_runner_timeout_terminates_and_reaps_child() -> None:
    runner = ProcessRunner(timeout_seconds=2, max_output_bytes=128, terminate_timeout_seconds=0.05)
    started = time.monotonic()
    with pytest.raises(ProcessTimeoutError):
        await runner.run(
            [sys.executable, "-c", "import time; time.sleep(60)"], timeout_seconds=0.05
        )

    assert time.monotonic() - started < 1


@pytest.mark.asyncio
async def test_runner_cancellation_terminates_and_reaps_child() -> None:
    runner = ProcessRunner(timeout_seconds=10, max_output_bytes=128, terminate_timeout_seconds=0.05)
    task = asyncio.create_task(runner.run([sys.executable, "-c", "import time; time.sleep(60)"]))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_runner_kills_a_child_that_ignores_terminate() -> None:
    runner = ProcessRunner(timeout_seconds=2, max_output_bytes=128, terminate_timeout_seconds=0.05)
    code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    with pytest.raises(ProcessTimeoutError):
        await runner.run([sys.executable, "-c", code], timeout_seconds=0.05)


@pytest.mark.asyncio
async def test_runner_stops_output_flood_without_unbounded_capture() -> None:
    runner = ProcessRunner(timeout_seconds=2, max_output_bytes=64, terminate_timeout_seconds=0.05)
    code = "import sys; sys.stdout.write('x' * 10000000); sys.stdout.flush()"
    with pytest.raises(ProcessOutputLimitError) as caught:
        await runner.run([sys.executable, "-c", code])
    assert caught.value.stream == "stdout"
    assert caught.value.limit == 64


@pytest.mark.asyncio
async def test_owned_process_wait_drains_captured_streams() -> None:
    runner = ProcessRunner(timeout_seconds=2, max_output_bytes=64, terminate_timeout_seconds=0.05)
    child = await runner.start(
        [sys.executable, "-c", "print('small')"],
    )
    assert await child.wait() == 0


@pytest.mark.asyncio
async def test_blocking_adapter_timeout_detaches_daemon_worker_and_calls_close_hook() -> None:
    closed = asyncio.Event()

    def hanging_call() -> None:
        time.sleep(60)

    with pytest.raises(ProcessTimeoutError):
        await run_blocking(
            hanging_call,
            timeout_seconds=0.01,
            on_abort=closed.set,
        )
    assert closed.is_set()


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="requires POSIX signals")
@pytest.mark.asyncio
async def test_runner_cleanup_is_idempotent() -> None:
    runner = ProcessRunner(timeout_seconds=2, max_output_bytes=128, terminate_timeout_seconds=0.05)
    child = await runner.start(
        [sys.executable, "-c", "import time; time.sleep(60)"], capture_output=False
    )
    first = await child.cleanup(reason="test")
    second = await child.cleanup(reason="repeat")
    assert first.terminated
    assert first.reaped
    assert second == first
    assert second.reaped
