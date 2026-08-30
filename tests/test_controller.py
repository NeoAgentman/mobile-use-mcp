from dataclasses import dataclass

import pytest
from PIL import Image

from mobile_use_mcp.android_client import AndroidClient
from mobile_use_mcp.controller import AndroidController
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import Target


class FakeU2Device:
    def __init__(self) -> None:
        self.focused = False

    @property
    def info(self) -> dict[str, object]:
        return {}

    def screenshot(self) -> Image.Image:
        return Image.new("RGB", (1080, 2400), "white")

    def dump_hierarchy(self, compressed: bool = True) -> str:
        focused = str(self.focused).lower()
        return f"""<hierarchy><node text="Continue" resource-id="com.example:id/continue"
        class="android.widget.Button" package="com.example" clickable="true" enabled="true"
        focusable="true" focused="{focused}" scrollable="false" selected="false"
        checkable="false" checked="false" bounds="[20,100][300,180]" /></hierarchy>"""

    def press(self, key: str) -> bool:
        return True

    def set_input_ime(self, enabled: bool = True) -> None:
        pass

    def send_keys(self, text: str) -> None:
        pass

    def clear_text(self) -> None:
        pass


@dataclass
class FakeRunningApp:
    package: str = "com.example"
    activity: str = ".MainActivity"


class FakeADBDevice:
    def __init__(self, u2_device: FakeU2Device | None = None) -> None:
        self.u2_device = u2_device
        self.clicks: list[tuple[int, int]] = []
        self.swipes: list[tuple[int, int, int, int, float]] = []
        self.keys: list[str] = []
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.urls: list[str] = []
        self.current = FakeRunningApp()

    def click(self, x: int, y: int, display_id: int | None = None) -> None:
        self.clicks.append((x, y))
        if self.u2_device is not None:
            self.u2_device.focused = True

    def swipe(self, sx: int, sy: int, ex: int, ey: int, duration: float = 1.0) -> None:
        self.swipes.append((sx, sy, ex, ey, duration))

    def keyevent(self, key_code: int | str) -> None:
        self.keys.append(str(key_code))

    def app_start(self, package_name: str, activity: str | None = None) -> None:
        self.started.append(package_name)
        self.current = FakeRunningApp(package=package_name)

    def app_stop(self, package_name: str) -> None:
        self.stopped.append(package_name)

    def open_browser(self, url: str) -> None:
        self.urls.append(url)

    def app_current(self) -> FakeRunningApp:
        return self.current

    def shell(
        self,
        cmdargs: list[str],
        stream: bool = False,
        timeout: float | None = 600,
        encoding: str | None = "utf-8",
        rstrip: bool = True,
    ) -> str:
        if cmdargs[:4] == ["pm", "list", "packages", "-3"]:
            return "package:com.example\npackage:org.demo\n"
        return ""


@pytest.fixture
def controller() -> tuple[AndroidController, FakeADBDevice]:
    u2_device = FakeU2Device()
    adb_device = FakeADBDevice(u2_device)
    android_client = AndroidClient("serial", connector=lambda _: u2_device)
    return (
        AndroidController(
            "serial",
            android_client=android_client,
            adb_connector=lambda _: adb_device,
        ),
        adb_device,
    )


async def test_tap_resolves_element_and_clicks_center(
    controller: tuple[AndroidController, FakeADBDevice],
) -> None:
    service, adb = controller

    result = await service.tap(Target(text="Continue"))

    assert adb.clicks == [(160, 140)]
    assert result["selector"] == "text='Continue'[0]"


async def test_swipe_validates_screen_bounds(
    controller: tuple[AndroidController, FakeADBDevice],
) -> None:
    service, adb = controller
    await service.swipe(100, 200, 100, 1000, duration_ms=500)
    assert adb.swipes == [(100, 200, 100, 1000, 0.5)]

    with pytest.raises(MobileUseError) as caught:
        await service.swipe(1200, 200, 100, 1000)
    assert caught.value.code == ErrorCode.INVALID_COORDINATES


async def test_key_url_and_app_operations(
    controller: tuple[AndroidController, FakeADBDevice],
) -> None:
    service, adb = controller

    await service.press_key("home")
    foreground = await service.launch_app("org.demo", retries=1, timeout_seconds=0.1)
    await service.terminate_app("org.demo")
    await service.open_url("https://example.com")

    assert adb.keys == ["HOME"]
    assert foreground.foreground_app.package == "org.demo"
    assert foreground.attempts[0].outcome == "ready"
    assert adb.stopped == ["org.demo"]
    assert adb.urls == ["https://example.com"]


async def test_launch_app_reports_blocking_foreground() -> None:
    u2_device = FakeU2Device()
    adb_device = FakeADBDevice(u2_device)
    adb_device.app_start = lambda package_name, activity=None: None  # type: ignore[method-assign]
    adb_device.current = FakeRunningApp(package="com.android.permissioncontroller")
    service = AndroidController(
        "serial",
        android_client=AndroidClient("serial", connector=lambda _: u2_device),
        adb_connector=lambda _: adb_device,
    )

    with pytest.raises(MobileUseError) as caught:
        await service.launch_app("org.demo", retries=1, timeout_seconds=0.1)

    assert caught.value.code == ErrorCode.TIMEOUT
    assert caught.value.data["last_foreground_app"] == {
        "package": "com.android.permissioncontroller",
        "activity": ".MainActivity",
    }
    assert caught.value.data["attempts"][0]["outcome"] == "blocked_by_other_app"  # type: ignore[index]


async def test_invalid_url_is_rejected(
    controller: tuple[AndroidController, FakeADBDevice],
) -> None:
    service, _ = controller
    with pytest.raises(MobileUseError):
        await service.open_url("file:///etc/passwd")


async def test_list_apps_filters_and_limits(
    controller: tuple[AndroidController, FakeADBDevice],
) -> None:
    service, _ = controller

    assert await service.list_apps(query="demo") == ["org.demo"]


class InventoryADBDevice(FakeADBDevice):
    """Small stateful ADB substitute for the deterministic app inventory seam."""

    def __init__(self) -> None:
        super().__init__()
        self.current = FakeRunningApp(package="com.android.launcher")

    def shell(
        self,
        cmdargs: list[str],
        stream: bool = False,
        timeout: float | None = 600,
        encoding: str | None = "utf-8",
        rstrip: bool = True,
    ) -> str:
        del stream, timeout, encoding, rstrip
        if cmdargs == ["pm", "list", "packages"]:
            return "package:com.example.demo\npackage:com.android.settings\n"
        if cmdargs == ["pm", "list", "packages", "-s"]:
            return "package:com.android.settings\n"
        if cmdargs == ["pm", "list", "packages", "-3"]:
            return "package:com.example.demo\n"
        if cmdargs == ["dumpsys", "package", "com.example.demo"]:
            return (
                "applicationInfo=ApplicationInfo{com.example.demo "
                "enabled=true nonLocalizedLabel=示例应用 flags=0x0}\n"
            )
        if cmdargs == ["dumpsys", "package", "com.android.settings"]:
            return "applicationInfo=ApplicationInfo{com.android.settings enabled=true flags=0x1}\n"
        if cmdargs[:3] == ["cmd", "package", "resolve-activity"]:
            package = cmdargs[-1]
            if package == "com.example.demo":
                return "com.example.demo/.MainActivity\n"
            return "No activity found\n"
        return ""


async def test_app_inventory_is_stable_pageable_and_searches_localized_labels() -> None:
    u2_device = FakeU2Device()
    adb_device = InventoryADBDevice()
    service = AndroidController(
        "serial",
        android_client=AndroidClient("serial", connector=lambda _: u2_device),
        adb_connector=lambda _: adb_device,
    )

    inventory = await service.list_app_inventory()

    assert [app.package for app in inventory.apps] == [
        "com.android.settings",
        "com.example.demo",
    ]
    assert inventory.apps[0].launcher_label is None
    assert inventory.apps[0].launchable_activity is None
    assert inventory.apps[0].is_system is True
    assert inventory.apps[0].is_third_party is False
    assert inventory.apps[1].launcher_label == "示例应用"
    assert inventory.apps[1].launchable_activity == ".MainActivity"
    assert inventory.apps[1].is_system is False
    assert inventory.apps[1].is_third_party is True
    assert inventory.apps[1].enabled is True

    localized = await service.list_app_inventory(query="示例应用")
    assert [app.package for app in localized.apps] == ["com.example.demo"]


async def test_foreground_read_failure_is_not_an_empty_foreground() -> None:
    u2_device = FakeU2Device()
    adb_device = FakeADBDevice(u2_device)

    def broken_current() -> FakeRunningApp:
        raise RuntimeError("dumpsys unavailable")

    adb_device.app_current = broken_current  # type: ignore[method-assign]
    service = AndroidController(
        "serial",
        android_client=AndroidClient("serial", connector=lambda _: u2_device),
        adb_connector=lambda _: adb_device,
    )

    with pytest.raises(MobileUseError) as caught:
        await service.get_foreground_app()

    assert caught.value.code == ErrorCode.FOREGROUND_UNAVAILABLE


async def test_termination_reports_verified_effect() -> None:
    u2_device = FakeU2Device()
    adb_device = FakeADBDevice(u2_device)
    adb_device.current = FakeRunningApp(package="org.demo")

    def stop(package: str) -> None:
        adb_device.stopped.append(package)
        adb_device.current = FakeRunningApp(package="com.android.launcher")

    adb_device.app_stop = stop  # type: ignore[method-assign]
    service = AndroidController(
        "serial",
        android_client=AndroidClient("serial", connector=lambda _: u2_device),
        adb_connector=lambda _: adb_device,
    )

    result = await service.terminate_app("org.demo")

    assert result.targeted_package == "org.demo"
    assert result.command_status == "sent"
    assert result.effect_status == "verified"


async def test_launch_records_transition_and_does_not_confuse_foreground_read_failure() -> None:
    u2_device = FakeU2Device()
    adb_device = FakeADBDevice(u2_device)
    states = iter(
        [
            FakeRunningApp(package="com.android.permissioncontroller"),
            FakeRunningApp(package="org.demo"),
            FakeRunningApp(package="org.demo"),
        ]
    )

    def current() -> FakeRunningApp:
        return next(states)

    adb_device.app_current = current  # type: ignore[method-assign]
    service = AndroidController(
        "serial",
        android_client=AndroidClient("serial", connector=lambda _: u2_device),
        adb_connector=lambda _: adb_device,
    )

    result = await service.launch_app("org.demo", retries=1, timeout_seconds=0.2)

    assert result.effect_status == "verified"
    assert result.stabilized is True
    assert result.blockers[0].package == "com.android.permissioncontroller"
    assert result.attempts[0].transitions[0].package == "com.android.permissioncontroller"


async def test_launch_foreground_read_failure_has_distinct_outcome() -> None:
    u2_device = FakeU2Device()
    adb_device = FakeADBDevice(u2_device)

    def broken_current() -> FakeRunningApp:
        raise RuntimeError("foreground unavailable")

    adb_device.app_current = broken_current  # type: ignore[method-assign]
    service = AndroidController(
        "serial",
        android_client=AndroidClient("serial", connector=lambda _: u2_device),
        adb_connector=lambda _: adb_device,
    )

    with pytest.raises(MobileUseError) as caught:
        await service.launch_app("org.demo", retries=1, timeout_seconds=0.2)

    assert caught.value.code == ErrorCode.FOREGROUND_UNAVAILABLE
    assert caught.value.data["attempts"][0]["outcome"] == "foreground_unavailable"  # type: ignore[index]
    assert adb_device.started == ["org.demo"]
    assert all(
        attempt["outcome"] != "loading_timeout"  # type: ignore[index]
        for attempt in caught.value.data["attempts"]  # type: ignore[union-attr]
    )


async def test_termination_is_unverified_when_target_was_not_foreground() -> None:
    u2_device = FakeU2Device()
    adb_device = FakeADBDevice(u2_device)
    adb_device.current = FakeRunningApp(package="com.other")
    service = AndroidController(
        "serial",
        android_client=AndroidClient("serial", connector=lambda _: u2_device),
        adb_connector=lambda _: adb_device,
    )

    result = await service.terminate_app("org.demo")

    assert result.command_status == "sent"
    assert result.effect_status == "unverified"
    assert result.effect_verified is False
    assert result.verification_reason == "target_package_was_not_foreground"


async def test_app_query_failure_is_distinct_from_an_empty_match() -> None:
    u2_device = FakeU2Device()
    adb_device = FakeADBDevice(u2_device)

    def broken_shell(
        cmdargs: list[str],
        stream: bool = False,
        timeout: float | None = 600,
        encoding: str | None = "utf-8",
        rstrip: bool = True,
    ) -> str:
        del cmdargs, stream, timeout, encoding, rstrip
        raise RuntimeError("adb query failed")

    adb_device.shell = broken_shell  # type: ignore[method-assign]
    service = AndroidController(
        "serial",
        android_client=AndroidClient("serial", connector=lambda _: u2_device),
        adb_connector=lambda _: adb_device,
    )

    with pytest.raises(MobileUseError) as caught:
        await service.list_app_inventory()

    assert caught.value.code == ErrorCode.APP_QUERY_FAILED


async def test_app_query_error_text_is_not_mistaken_for_an_empty_inventory() -> None:
    u2_device = FakeU2Device()
    adb_device = FakeADBDevice(u2_device)

    def shell(
        cmdargs: list[str],
        stream: bool = False,
        timeout: float | None = 600,
        encoding: str | None = "utf-8",
        rstrip: bool = True,
    ) -> str:
        del cmdargs, stream, timeout, encoding, rstrip
        return "Error: device offline\n"

    adb_device.shell = shell  # type: ignore[method-assign]
    service = AndroidController(
        "serial",
        android_client=AndroidClient("serial", connector=lambda _: u2_device),
        adb_connector=lambda _: adb_device,
    )

    with pytest.raises(MobileUseError) as caught:
        await service.list_app_inventory()

    assert caught.value.code == ErrorCode.APP_QUERY_FAILED


async def test_app_inventory_preserves_unknown_metadata_when_metadata_is_unavailable() -> None:
    u2_device = FakeU2Device()
    adb_device = FakeADBDevice(u2_device)

    def shell(
        cmdargs: list[str],
        stream: bool = False,
        timeout: float | None = 600,
        encoding: str | None = "utf-8",
        rstrip: bool = True,
    ) -> str:
        del stream, timeout, encoding, rstrip
        if cmdargs == ["pm", "list", "packages"]:
            return "package:com.example.demo\n"
        if cmdargs == ["pm", "list", "packages", "-s"]:
            return ""
        if cmdargs == ["pm", "list", "packages", "-3"]:
            return "package:com.example.demo\n"
        raise RuntimeError(f"unsupported metadata command: {cmdargs}")

    adb_device.shell = shell  # type: ignore[method-assign]
    service = AndroidController(
        "serial",
        android_client=AndroidClient("serial", connector=lambda _: u2_device),
        adb_connector=lambda _: adb_device,
    )

    inventory = await service.list_app_inventory()

    assert inventory.metadata_status == "unavailable"
    assert inventory.apps[0].launcher_label is None
    assert inventory.apps[0].launchable_activity is None
    assert inventory.apps[0].is_system is False
    assert inventory.apps[0].is_third_party is True
    assert inventory.apps[0].enabled is None


async def test_type_text_can_focus_and_verify_target(
    controller: tuple[AndroidController, FakeADBDevice],
) -> None:
    service, adb = controller

    method = await service.type_text(
        "hello",
        Target(resource_id="com.example:id/continue"),
    )

    assert method == "uiautomator2"
    assert adb.clicks == [(160, 140)]


async def test_clear_text_prefers_uiautomator2(
    controller: tuple[AndroidController, FakeADBDevice],
) -> None:
    service, adb = controller

    method = await service.clear_text(20)

    assert method == "uiautomator2"
    assert adb.keys == []
