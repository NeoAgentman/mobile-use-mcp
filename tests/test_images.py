from io import BytesIO

from PIL import Image

from mobile_use_mcp.images import encode_screenshot


def _png(width: int = 1000, height: int = 2000) -> bytes:
    output = BytesIO()
    Image.new("RGBA", (width, height), (10, 20, 30, 128)).save(output, format="PNG")
    return output.getvalue()


def test_encode_screenshot_preserves_png_dimensions_by_default() -> None:
    data, width, height = encode_screenshot(_png())
    assert data.startswith(b"\x89PNG")
    assert (width, height) == (1000, 2000)


def test_encode_screenshot_downscales_and_converts_to_jpeg() -> None:
    data, width, height = encode_screenshot(
        _png(), image_format="jpeg", image_quality=70, max_width=500
    )
    assert data.startswith(b"\xff\xd8")
    assert (width, height) == (500, 1000)
    with Image.open(BytesIO(data)) as image:
        assert image.mode == "RGB"
