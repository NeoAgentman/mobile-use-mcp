from unittest.mock import Mock

import pytest
from PIL import Image

import mobile_use_mcp.doctor as doctor
from mobile_use_mcp.config import RuntimeConfig
from mobile_use_mcp.devices import parse_adb_devices
from mobile_use_mcp.doctor import CHECK_NAMES, run_doctor
from mobile_use_mcp.models import DoctorStatus


class FakeU2Device:
    @property
    def info(self) -> dict[str, object]:
        return {"displayWidth": 100}

    def screenshot(self) -> Image.Image:
        return Image.new("RGB", (100, 100), "white")

    def dump_hierarchy(self, compressed: bool = True) -> str:
        assert compressed
        return "<hierarchy />"

    def press(self, key: str) -> bool:
        return True

    def set_input_ime(self, enabled: bool = True) -> None:
        return None

    def send_keys(self, text: str) -> None:
        return None

    def clear_text(self) -> None:
        return None


class FakeRegistry:
    def __init__(self, devices: str, screenrecord: str = "/system/bin/screenrecord") -> None:
        self.devices = parse_adb_devices(devices)
        self.screenrecord = screenrecord

    def check_adb(self) -> str:
        return "available"

    def list_devices(self):
        return self.devices

    def shell(self, serial: str, arguments: tuple[str, ...]) -> str:
        assert serial
        assert arguments == ("command", "-v", "screenrecord")
        return self.screenrecord


def test_doctor_reports_independent_ready_checks_for_one_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = FakeRegistry("List of devices attached\nABC device\n")

    def available(name: str) -> str:
        return "/bin/" + name

    monkeypatch.setattr(doctor.shutil, "which", available)

    result = run_doctor(
        RuntimeConfig.defaults(),
        registry=registry,  # type: ignore[arg-type]
        u2_connector=lambda serial: FakeU2Device(),
        operation_id="op-test",
    )

    assert result.operation_id == "op-test"
    assert [check.name for check in result.checks] == list(CHECK_NAMES)
    assert result.overall_status == DoctorStatus.READY
    assert result.success


def test_doctor_never_selects_implicitly_when_multiple_devices_are_online(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = FakeRegistry("List of devices attached\nA device\nB device\n")
    connector = Mock(side_effect=AssertionError("must not connect without serial"))

    def unavailable(name: str) -> None:
        return None

    monkeypatch.setattr(doctor.shutil, "which", unavailable)

    result = run_doctor(
        RuntimeConfig.defaults(),
        registry=registry,  # type: ignore[arg-type]
        u2_connector=connector,
    )

    checks = {check.name: check for check in result.checks}
    assert checks["device"].status == DoctorStatus.DEGRADED
    assert checks["uiautomator2"].status == DoctorStatus.UNAVAILABLE
    assert checks["screenshot"].status == DoctorStatus.UNAVAILABLE
    connector.assert_not_called()
