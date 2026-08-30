from __future__ import annotations

from html import escape

import pytest
from PIL import Image

from mobile_use_mcp.android_client import AndroidClient
from mobile_use_mcp.controller import AndroidController
from mobile_use_mcp.errors import ErrorCode, MobileUseError


class _InputDevice:
    def __init__(self, *, text: str = "", password: bool = False) -> None:
        self.text = text
        self.password = password
        self.focused = True
        self.sent: list[str] = []
        self.ime_states: list[bool] = []
        self.fail_after_send = False
        self.fail_restore = False
        self.clear_is_noop = False

    @property
    def info(self) -> dict[str, object]:
        return {}

    def screenshot(self) -> Image.Image:
        return Image.new("RGB", (100, 200), "white")

    def dump_hierarchy(self, compressed: bool = True) -> str:
        del compressed
        text = "•" * len(self.text) if self.password else self.text
        return (
            "<hierarchy>"
            f'<node text="{escape(text)}" resource-id="pkg:id/input" '
            'class="android.widget.EditText" package="pkg" clickable="true" '
            'enabled="true" focusable="true" '
            f'focused="{str(self.focused).lower()}" password="{str(self.password).lower()}" '
            'bounds="[0,0][90,30]" />'
            "</hierarchy>"
        )

    def press(self, key: str) -> bool:
        del key
        return True

    def set_input_ime(self, enabled: bool = True) -> None:
        self.ime_states.append(enabled)
        if not enabled and self.fail_restore:
            raise RuntimeError("restore failed")

    def send_keys(self, text: str) -> None:
        self.sent.append(text)
        self.text += text
        if self.fail_after_send:
            raise RuntimeError("send failed after dispatch")

    def clear_text(self) -> None:
        if not self.clear_is_noop:
            self.text = ""


class _Foreground:
    package = "pkg"
    activity = ".Main"


class _ADB:
    def __init__(self, device: _InputDevice) -> None:
        self.device = device
        self.shell_calls: list[list[str]] = []
        self.keys: list[str] = []

    def click(self, x: int, y: int, display_id: int | None = None) -> None:
        del x, y, display_id
        self.device.focused = True

    def swipe(
        self,
        sx: int,
        sy: int,
        ex: int,
        ey: int,
        duration: float = 1.0,
    ) -> None:
        del sx, sy, ex, ey, duration

    def keyevent(self, key_code: int | str) -> None:
        self.keys.append(str(key_code))
        if key_code == "DEL":
            self.device.text = self.device.text[:-1]

    def app_start(self, package_name: str, activity: str | None = None) -> None:
        del package_name, activity

    def app_stop(self, package_name: str) -> None:
        del package_name

    def open_browser(self, url: str) -> None:
        del url

    def app_current(self) -> _Foreground:
        return _Foreground()

    def shell(
        self,
        cmdargs: list[str],
        stream: bool = False,
        timeout: float | None = 600,
        encoding: str | None = "utf-8",
        rstrip: bool = True,
    ) -> str:
        del stream, timeout, encoding, rstrip
        self.shell_calls.append(cmdargs)
        if cmdargs[:2] == ["input", "text"]:
            self.device.text += cmdargs[2]
        return ""


def _controller(device: _InputDevice) -> tuple[AndroidController, _ADB]:
    adb = _ADB(device)
    return (
        AndroidController(
            "serial",
            android_client=AndroidClient("serial", connector=lambda _: device),
            adb_connector=lambda _: adb,
        ),
        adb,
    )


@pytest.mark.asyncio
async def test_type_text_reports_verified_unicode_spaces_and_specials() -> None:
    device = _InputDevice(text="prefix")
    controller, adb = _controller(device)
    value = " 世界 with spaces !?& "

    result = await controller.type_text(value)

    assert result.method == "uiautomator2"
    assert result.command_status == "sent"
    assert result.effect_status == "verified"
    assert result.restoration_status == "restored"
    assert result.verification_available is True
    assert adb.shell_calls == []
    assert value not in str(result.to_data())


@pytest.mark.asyncio
async def test_uncertain_input_is_not_sent_again_through_adb() -> None:
    device = _InputDevice()
    device.fail_after_send = True
    controller, adb = _controller(device)
    secret = "secret-123"

    with pytest.raises(MobileUseError) as caught:
        await controller.type_text(secret)

    assert caught.value.code == ErrorCode.INPUT_EXECUTION_UNCERTAIN
    assert adb.shell_calls == []
    assert secret not in str(caught.value)
    assert secret not in str(caught.value.data)


@pytest.mark.asyncio
async def test_definite_no_effect_after_adapter_failure_allows_one_fallback() -> None:
    device = _InputDevice()
    original_send = device.send_keys

    def failed_before_send(text: str) -> None:
        del text
        raise RuntimeError("send unavailable")

    device.send_keys = failed_before_send  # type: ignore[method-assign]
    controller, adb = _controller(device)

    result = await controller.type_text("fallback")

    assert result.method == "adb"
    assert result.fallback_used is True
    assert result.effect_status == "verified"
    assert adb.shell_calls == [["input", "text", "fallback"]]
    device.send_keys = original_send  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_restore_failure_does_not_trigger_duplicate_text_fallback() -> None:
    device = _InputDevice()
    device.fail_restore = True
    controller, adb = _controller(device)
    secret = "private-value"

    with pytest.raises(MobileUseError) as caught:
        await controller.type_text(secret)

    assert caught.value.code == ErrorCode.INPUT_METHOD_RESTORE_FAILED
    assert caught.value.data["command_status"] == "sent"
    assert caught.value.data["restoration_status"] == "failed"
    assert adb.shell_calls == []
    assert secret not in str(caught.value.data)


@pytest.mark.asyncio
async def test_password_like_field_is_not_claimed_as_verified() -> None:
    device = _InputDevice(password=True)
    controller, _adb = _controller(device)

    result = await controller.type_text("verification-code")

    assert result.command_status == "sent"
    assert result.effect_status == "unverified"
    assert result.verification_available is False
    assert result.requires_observation is True


@pytest.mark.asyncio
async def test_clear_text_verifies_empty_state() -> None:
    device = _InputDevice(text="existing")
    controller, adb = _controller(device)

    result = await controller.clear_text(3)

    assert result.effect_status == "verified"
    assert result.command_status == "sent"
    assert result.fallback_used is False
    assert adb.keys == []


@pytest.mark.asyncio
async def test_clear_text_fallback_is_bounded() -> None:
    device = _InputDevice(text="abcdef")
    device.clear_is_noop = True
    controller, adb = _controller(device)

    result = await controller.clear_text(3)

    assert result.method == "adb"
    assert result.fallback_used is True
    assert result.attempts == 3
    assert result.max_attempts == 3
    assert adb.keys == ["DEL", "DEL", "DEL"]
    assert result.effect_status == "not_empty"
    assert result.effect_verified is False


@pytest.mark.asyncio
async def test_clear_text_unverifiable_is_not_verified() -> None:
    device = _InputDevice(password=True)
    controller, _adb = _controller(device)

    result = await controller.clear_text(2)

    assert result.effect_status == "unverified"
    assert result.effect_verified is False
    assert result.requires_observation is True
