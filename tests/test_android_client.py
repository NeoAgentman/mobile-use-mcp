from PIL import Image

from mobile_use_mcp.android_client import AndroidClient


class FakeDevice:
    def __init__(self) -> None:
        self.alive = True
        self.fast_input_states: list[bool] = []
        self.sent_text: list[str] = []
        self.pressed: list[str] = []
        self.clear_count = 0

    @property
    def info(self) -> dict[str, object]:
        if not self.alive:
            raise RuntimeError("disconnected")
        return {"displayWidth": 1080}

    def screenshot(self) -> Image.Image:
        return Image.new("RGB", (1080, 2400), "white")

    def dump_hierarchy(self, compressed: bool = True) -> str:
        assert compressed
        return '<hierarchy><node text="Home" bounds="[0,0][100,100]" /></hierarchy>'

    def press(self, key: str) -> bool:
        self.pressed.append(key)
        return True

    def set_input_ime(self, enabled: bool = True) -> None:
        self.fast_input_states.append(enabled)

    def send_keys(self, text: str) -> None:
        self.sent_text.append(text)

    def clear_text(self) -> None:
        self.clear_count += 1


def test_get_screen_returns_png_and_hierarchy() -> None:
    fake = FakeDevice()
    client = AndroidClient("serial", connector=lambda _: fake)

    screenshot, hierarchy, width, height = client.get_screen()

    assert screenshot.startswith(b"\x89PNG")
    assert "Home" in hierarchy
    assert (width, height) == (1080, 2400)


def test_send_text_restores_input_method() -> None:
    fake = FakeDevice()
    client = AndroidClient("serial", connector=lambda _: fake)

    client.send_text("hello 世界")

    assert fake.sent_text == ["hello 世界"]
    assert fake.fast_input_states == [False]


def test_clear_text_restores_input_method() -> None:
    fake = FakeDevice()
    client = AndroidClient("serial", connector=lambda _: fake)

    client.clear_text()

    assert fake.clear_count == 1
    assert fake.fast_input_states == [False]


def test_client_reconnects_once_after_health_check_failure() -> None:
    first = FakeDevice()
    second = FakeDevice()
    devices = iter([first, second])
    client = AndroidClient("serial", connector=lambda _: next(devices))
    client.connect()
    first.alive = False

    assert client.ensure_connected() is second
