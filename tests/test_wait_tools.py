import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mobile_use_mcp.config import RuntimeConfig
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import ForegroundApp, ScreenSnapshot, Target, UIElement, WaitResult
from mobile_use_mcp.server import (
    android_wait,
    android_wait_for_element,
    android_wait_for_text,
    android_wait_for_ui_change,
    mcp,
)
from mobile_use_mcp.session import DeviceSession


def _snapshot(
    *,
    elements: list[UIElement] | None = None,
    image: bytes = b"same",
) -> ScreenSnapshot:
    return ScreenSnapshot(
        serial="ABC",
        width=100,
        height=200,
        screenshot_png=image,
        elements=elements or [],
        foreground_app=ForegroundApp(package="com.example", activity=".Main"),
    )


def _wait_session(monkeypatch: pytest.MonkeyPatch, controller: Mock) -> DeviceSession:
    wait_session = DeviceSession(
        config=RuntimeConfig(operation_timeout_seconds=1, queue_timeout_seconds=1)
    )
    monkeypatch.setattr(wait_session, "require_controller", lambda: controller)
    monkeypatch.setattr("mobile_use_mcp.server.session", wait_session)
    return wait_session


async def test_condition_wait_tools_are_registered_with_bounded_defaults() -> None:
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert {
        "android_wait",
        "android_wait_for_text",
        "android_wait_for_element",
        "android_wait_for_ui_change",
    } <= names
    wait_tool = next(tool for tool in tools if tool.name == "android_wait_for_text")
    properties = wait_tool.inputSchema["properties"]
    assert properties["timeout_seconds"]["default"] == 10.0  # type: ignore[index]
    assert properties["poll_interval_seconds"]["default"] == 0.25  # type: ignore[index]
    assert "complete Android hierarchy" in str(properties["text"])  # type: ignore[index]


async def test_condition_wait_is_exposed_through_stdio() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mobile_use_mcp"],
        cwd=project_root,
    )

    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        result = await client.call_tool(
            "android_wait_for_text",
            arguments={"text": "Ready", "timeout_seconds": 0.2, "poll_interval_seconds": 0.02},
        )

    assert result.isError is True
    assert result.structuredContent is not None
    assert result.structuredContent["error_code"] == ErrorCode.NOT_CONNECTED.value


async def test_wait_for_text_returns_final_immutable_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = Mock()
    controller.snapshot = AsyncMock(
        return_value=_snapshot(elements=[UIElement(text="Ready", clickable=True)])
    )
    wait_session = _wait_session(monkeypatch, controller)

    result = await android_wait_for_text("ready", timeout_seconds=0.5, poll_interval_seconds=0.01)

    assert isinstance(result, WaitResult)
    assert result.success is True
    assert result.poll_count == 1
    assert result.snapshot_id is not None
    assert result.matched_element is None
    assert "ready" not in json.dumps(result.model_dump(mode="json")).casefold()
    assert wait_session.get_snapshot(result.snapshot_id).snapshot_id == result.snapshot_id
    controller.snapshot.assert_awaited_once_with(
        interactive_only=False,
        max_elements=None,
        max_text_length=500,
    )


async def test_wait_for_text_polls_until_delayed_match(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = Mock()
    controller.snapshot = AsyncMock(
        side_effect=[
            _snapshot(elements=[UIElement(text="Loading")]),
            _snapshot(elements=[UIElement(text="Ready")]),
        ]
    )
    _wait_session(monkeypatch, controller)

    result = await android_wait_for_text("ready", timeout_seconds=0.5, poll_interval_seconds=0.01)

    assert result.success is True
    assert result.poll_count == 2
    assert controller.snapshot.await_count == 2


async def test_wait_for_element_reads_complete_hierarchy(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = Mock()
    elements = [UIElement(text=f"Item {index}") for index in range(250)]
    controller.snapshot = AsyncMock(return_value=_snapshot(elements=elements))
    _wait_session(monkeypatch, controller)

    result = await android_wait_for_element(
        Target(text="Item 249"),
        timeout_seconds=0.5,
        poll_interval_seconds=0.01,
    )

    assert result.success is True
    assert result.matched_element is not None
    assert result.matched_element.text == "Item 249"
    controller.snapshot.assert_awaited_once()
    assert controller.snapshot.await_args.kwargs["max_elements"] is None


async def test_wait_for_element_preserves_target_text_selector_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = Mock()
    controller.snapshot = AsyncMock(
        side_effect=[
            _snapshot(elements=[UIElement(text="Not Ready")]),
            _snapshot(elements=[UIElement(text="Ready")]),
        ]
    )
    _wait_session(monkeypatch, controller)

    result = await android_wait_for_element(
        Target(text="Ready"),
        timeout_seconds=0.5,
        poll_interval_seconds=0.01,
    )

    assert result.success is True
    assert result.poll_count == 2


async def test_wait_for_ui_change_compares_explicit_baseline_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = Mock()
    controller.snapshot = AsyncMock(
        return_value=_snapshot(elements=[UIElement(text="Changed")], image=b"changed")
    )
    wait_session = _wait_session(monkeypatch, controller)
    baseline_id = wait_session.store_snapshot(_snapshot(elements=[UIElement(text="Before")]))

    result = await android_wait_for_ui_change(
        baseline_snapshot_id=baseline_id,
        timeout_seconds=0.5,
        poll_interval_seconds=0.01,
    )

    assert result.success is True
    assert result.baseline_snapshot_id == baseline_id
    assert result.snapshot_id != baseline_id


async def test_wait_for_ui_change_uses_current_session_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = Mock()
    controller.snapshot = AsyncMock(return_value=_snapshot(image=b"same"))
    wait_session = _wait_session(monkeypatch, controller)
    baseline_id = wait_session.store_snapshot(_snapshot(image=b"same"))
    baseline = wait_session.get_snapshot(baseline_id)
    wait_session.advance_screen_revision()

    result = await android_wait_for_ui_change(
        baseline_screen_revision=baseline.screen_revision,
        timeout_seconds=0.5,
        poll_interval_seconds=0.01,
    )

    assert result.success is True
    assert result.baseline_screen_revision == baseline.screen_revision
    assert result.screen_revision == baseline.screen_revision + 1


async def test_wait_timeout_is_mcp_error_with_poll_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = Mock()
    controller.snapshot = AsyncMock(return_value=_snapshot(elements=[UIElement(text="Loading")]))
    _wait_session(monkeypatch, controller)

    result = await android_wait_for_text("never", timeout_seconds=0.08, poll_interval_seconds=0.02)

    assert result.isError is True
    assert result.structuredContent is not None
    payload = result.structuredContent
    assert payload["success"] is False
    assert payload["error_code"] == ErrorCode.TIMEOUT.value
    assert payload["poll_count"] >= 1
    assert payload["elapsed_ms"] >= 0
    assert payload["last_observation"]["element_count"] == 1  # type: ignore[index]
    assert payload["last_error_stage"] == "condition"


async def test_wait_timeout_during_slow_observation_keeps_poll_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = Mock()

    async def slow_snapshot(**_kwargs: object) -> ScreenSnapshot:
        await asyncio.sleep(10)
        return _snapshot()

    controller.snapshot = slow_snapshot
    _wait_session(monkeypatch, controller)

    result = await android_wait_for_text("never", timeout_seconds=0.05, poll_interval_seconds=0.01)

    assert result.isError is True
    assert result.structuredContent is not None
    assert result.structuredContent["error_code"] == ErrorCode.TIMEOUT.value
    assert result.structuredContent["poll_count"] == 1
    assert result.structuredContent["last_error_stage"] == "execution"


async def test_wait_queue_timeout_reports_gateway_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = Mock()
    controller.snapshot = AsyncMock(return_value=_snapshot())
    wait_session = _wait_session(monkeypatch, controller)
    started = asyncio.Event()

    async def hold_operation(context: object) -> None:
        started.set()
        await asyncio.sleep(0.2)

    holder = asyncio.create_task(
        wait_session.run_operation(
            "hold",
            hold_operation,
            timeout_seconds=0.5,
            retry_safe=True,
            mutating=False,
        )
    )
    await started.wait()
    result = await android_wait_for_text("never", timeout_seconds=0.05, poll_interval_seconds=0.01)
    await holder

    assert result.isError is True
    assert result.structuredContent is not None
    assert result.structuredContent["error_code"] == ErrorCode.TIMEOUT.value
    assert result.structuredContent["last_error_stage"] == "queue"


async def test_disconnect_during_wait_returns_promptly_without_late_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = Mock()
    controller.snapshot = AsyncMock(return_value=_snapshot())
    wait_session = _wait_session(monkeypatch, controller)
    wait_session.require_controller = Mock(
        side_effect=MobileUseError(
            ErrorCode.DEVICE_DISCONNECTED,
            "device disconnected",
            "Reconnect the device.",
        )
    )

    result = await android_wait_for_text("never", timeout_seconds=1, poll_interval_seconds=0.1)

    assert result.isError is True
    assert result.structuredContent is not None
    assert result.structuredContent["error_code"] == ErrorCode.DEVICE_DISCONNECTED.value
    assert wait_session.snapshot_store.count == 0
    controller.snapshot.assert_not_awaited()


async def test_explicit_disconnect_unblocks_queued_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    controller = Mock()

    async def waiting_snapshot(**_kwargs: object) -> ScreenSnapshot:
        started.set()
        return _snapshot()

    controller.snapshot = waiting_snapshot
    controller.disconnect = Mock()
    wait_session = _wait_session(monkeypatch, controller)
    wait_task = asyncio.create_task(
        android_wait_for_text("never", timeout_seconds=5, poll_interval_seconds=1)
    )
    await started.wait()
    close_task = asyncio.create_task(wait_session.close())
    while not wait_session.disconnect_requested:
        await asyncio.sleep(0)

    wait_result, close_result = await asyncio.gather(wait_task, close_task)

    assert wait_result.isError is True
    assert wait_result.structuredContent is not None
    assert wait_result.structuredContent["error_code"] == ErrorCode.DEVICE_DISCONNECTED.value
    assert close_result.success is True
    assert wait_session.connected is False


async def test_cancelling_running_wait_does_not_commit_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    controller = Mock()

    async def slow_snapshot(**_kwargs: object) -> ScreenSnapshot:
        started.set()
        await asyncio.sleep(10)
        return _snapshot(elements=[UIElement(text="late")])

    controller.snapshot = slow_snapshot
    wait_session = _wait_session(monkeypatch, controller)
    task = asyncio.create_task(
        android_wait_for_text("late", timeout_seconds=1, poll_interval_seconds=0.01)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert wait_session.snapshot_store.count == 0


async def test_fixed_delay_remains_distinct_from_condition_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = Mock()
    controller.snapshot = AsyncMock(return_value=_snapshot(elements=[UIElement(text="Ready")]))
    wait_session = _wait_session(monkeypatch, controller)

    result = await android_wait(milliseconds=1)

    assert result["success"] is True
    assert result["data"]["milliseconds"] == 1  # type: ignore[index]
    assert wait_session.snapshot_store.count == 0
