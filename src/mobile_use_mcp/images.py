"""Bounded screenshot encoding for MCP image responses."""

from io import BytesIO
from typing import Literal

from PIL import Image

ImageFormat = Literal["png", "jpeg"]


def encode_screenshot(
    screenshot_png: bytes,
    *,
    image_format: ImageFormat = "png",
    image_quality: int = 80,
    max_width: int | None = None,
) -> tuple[bytes, int, int]:
    """Encode a PNG screenshot with optional downscaling and JPEG compression."""

    with Image.open(BytesIO(screenshot_png)) as source:
        image = source.copy()
    if max_width is not None and image.width > max_width:
        height = max(1, round(image.height * max_width / image.width))
        size: tuple[int, int] = (max_width, height)
        image = image.resize(size, Image.Resampling.LANCZOS)  # pyright: ignore[reportUnknownMemberType]

    output = BytesIO()
    if image_format == "jpeg":
        if image.mode not in {"RGB", "L"}:
            background = Image.new("RGB", image.size, "white")
            if "A" in image.getbands():
                background.paste(image, mask=image.getchannel("A"))
            else:
                background.paste(image)
            image = background
        image.save(output, format="JPEG", quality=image_quality, optimize=True)
    else:
        image.save(output, format="PNG", optimize=True)
    return output.getvalue(), image.width, image.height
