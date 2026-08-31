import asyncio
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock

import pytest

from mobile_use_mcp.config import ConfigurationError, RuntimeConfig, format_startup_error
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import DeviceInfo, DeviceState
from mobile_use_mcp.observability import hash_device_serial, redact_value
from mobile_use_mcp.session import DeviceSession


def _events(value: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in value.splitlines() if line]


def test_redaction_helper_is_conservative_for_content_and_identity() -> None:
    secret = "typed-secret"
    value = redact_value(
        {
            "typed_text": secret,
            "ui_text": "visible label",
            "serial": "ABC123",
            "safe_count": 2,
        }
    )

    assert value == {
        "typed_text": "[REDACTED]",
        "ui_text": "[REDACTED]",
        "serial": "[REDACTED]",
        "safe_count": 2,
    }
    assert secret not in json.dumps(value)


@pytest.mark.asyncio
async def test_operation_success_logs_only_stable_metadata_to_stderr() -> None:
    session = DeviceSession(config=RuntimeConfig(log_level="INFO"))
    stderr = io.StringIO()
    stdout = io.StringIO()

    with redirect_stdout(stdout), redirect_stderr(stderr):
        result = await session.run_operation(
            "status",
            lambda context: session.status(context.operation_id),
            operation_id="op-observe-success",
            retry_safe=True,
            mutating=False,
        )

    assert result.success is True
    assert stdout.getvalue() == ""
    events = _events(stderr.getvalue())
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "operation_finished"
    assert event["operation_id"] == "op-observe-success"
    assert event["operation"] == "status"
    assert event["result"] == "success"
    assert event["stage"] == "execution"
    assert isinstance(event["duration_ms"], float)
    assert event["error_code"] is None


@pytest.mark.asyncio
async def test_operation_failure_logs_error_code_and_hashed_serial_without_sensitive_values() -> (
    None
):
    registry = Mock()
    registry.select.return_value = DeviceInfo(serial="SERIAL-PRIVATE", state=DeviceState.DEVICE)
    controller = Mock()
    session = DeviceSession(
        registry=registry,
        controller_factory=lambda _serial: controller,
        config=RuntimeConfig(log_level="INFO"),
    )
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        await session.run_operation(
            "connect",
            lambda context: session.connect("SERIAL-PRIVATE", operation=context),
            operation_id="op-observe-connect",
        )
        with pytest.raises(MobileUseError):
            await session.run_operation(
                "private-input",
                lambda _context: (_ for _ in ()).throw(
                    MobileUseError(ErrorCode.OPERATION_FAILED, "raw-private-exception")
                ),
                operation_id="op-observe-failure",
            )

    events = _events(stderr.getvalue())
    failure = next(item for item in events if item["operation_id"] == "op-observe-failure")
    assert failure["result"] == "failure"
    assert failure["error_code"] == ErrorCode.OPERATION_FAILED.value
    assert failure["device_serial_hash"] == hash_device_serial("SERIAL-PRIVATE")
    rendered = stderr.getvalue()
    assert "SERIAL-PRIVATE" not in rendered
    assert "raw-private-exception" not in rendered


def test_status_exposes_bounded_device_health_summary() -> None:
    registry = Mock()
    registry.select.return_value = DeviceInfo(
        serial="SERIAL-PRIVATE",
        state=DeviceState.DEVICE,
        model="Pixel",
        product="oriole",
    )
    session = DeviceSession(registry=registry, controller_factory=lambda _serial: Mock())
    session.connect("SERIAL-PRIVATE")

    status = session.status("op-status")

    assert status.device_health.state == DeviceState.DEVICE
    assert status.device_health.adb_state == DeviceState.DEVICE
    assert status.device_health.connected is True
    assert status.device_health.serial_hash == hash_device_serial("SERIAL-PRIVATE")
    assert status.device_health.model == "Pixel"
    assert status.device_health.product == "oriole"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["cancel", "timeout"])
async def test_operation_cancellation_and_timeout_are_redacted_and_structured(kind: str) -> None:
    config = RuntimeConfig(log_level="INFO", operation_timeout_seconds=0.03)
    session = DeviceSession(config=config)
    stderr = io.StringIO()

    async def slow(_context: object) -> None:
        await asyncio.sleep(1)

    with redirect_stderr(stderr):
        task = asyncio.create_task(
            session.run_operation(
                f"operation-{kind}",
                slow,
                operation_id=f"op-observe-{kind}",
                retry_safe=False,
            )
        )
        if kind == "cancel":
            for _ in range(10):
                if session.active_operation_id is not None:
                    break
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(MobileUseError) as caught:
                await task
            assert caught.value.code == ErrorCode.TIMEOUT

        # A cancelled callback retains FIFO ownership through its cleanup.
        # Queueing a barrier proves the finish hook ran before we inspect it.
        await session.run_operation(
            "observability-barrier",
            lambda _context: None,
            operation_id=f"op-observe-{kind}-barrier",
            retry_safe=True,
            mutating=False,
        )

    events = _events(stderr.getvalue())
    event = next(item for item in events if item["operation_id"] == f"op-observe-{kind}")
    assert event["result"] == ("cancelled" if kind == "cancel" else "timeout")
    assert event["error_code"] in {ErrorCode.CANCELLED.value, ErrorCode.TIMEOUT.value}
    assert "typed-secret" not in stderr.getvalue()


def test_content_trace_is_disabled_by_default_and_requires_bounds() -> None:
    defaults = RuntimeConfig.defaults()
    assert defaults.content_trace_enabled is False
    assert defaults.content_trace_redaction == "strict"
    assert defaults.content_trace_retention_seconds == 0
    assert defaults.content_trace_max_bytes == 0

    with pytest.raises(ConfigurationError) as caught:
        RuntimeConfig.from_environment(
            {
                "MOBILE_USE_CONTENT_TRACE_ENABLED": "true",
                "MOBILE_USE_CONTENT_TRACE_REDACTION": "none",
            }
        )
    message = format_startup_error(caught.value)
    assert "CONTENT_TRACE" not in message
    assert "content_trace" in message

    with pytest.raises(ConfigurationError):
        RuntimeConfig.from_environment(
            {
                "MOBILE_USE_CONTENT_TRACE_ENABLED": "true",
                "MOBILE_USE_CONTENT_TRACE_RETENTION_SECONDS": "60",
            }
        )
