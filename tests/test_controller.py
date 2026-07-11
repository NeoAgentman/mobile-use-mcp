from dataclasses import dataclass

import pytest
from PIL import Image

from mobile_use_mcp.android_client import AndroidClient
from mobile_use_mcp.controller import AndroidController
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import Target


class FakeU2Device:
    @property
    def info(self) -> dict[str, object]:
        return {}

    def screenshot(self) -> Image.Image:
        return Image.new("RGB", (1080, 2400), "white")

    def dump_hierarchy(self, compressed: bool = True) -> str:
        return """<hierarchy><node text="Continue" resource-id="com.example:id/continue"
        class="android.widget.Button" package="com.example" clickable="true" enabled="true"
        focusable="true" focused="false" scrollable="false" selected="false"
        checkable="false" checked="false" bounds="[20,100][300,180]" /></hierarchy>"""

    def press(self, key: str) -> bool:
        return True

    def set_fastinput_ime(self, enabled: bool) -> None:
        pass

    def send_keys(self, text: str) -> None:
        pass


@dataclass
class FakeRunningApp:
    package: str = "com.example"
    activity: str = ".MainActivity"


class FakeADBDevice:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []
        self.swipes: list[tuple[int, int, int, int, float]] = []
        self.keys: list[str] = []
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.urls: list[str] = []
        self.current = FakeRunningApp()

    def click(self, x: int, y: int, display_id: int | None = None) -> None:
        self.clicks.append((x, y))

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
    adb_device = FakeADBDevice()
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
    assert foreground.package == "org.demo"
    assert adb.stopped == ["org.demo"]
    assert adb.urls == ["https://example.com"]


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
