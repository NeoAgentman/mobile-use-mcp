"""Original-resolution screenshot encoding for MCP image responses."""

from io import BytesIO
from typing import Literal

from PIL import Image

ImageFormat = Literal["png", "jpeg"]


def encode_screenshot(
    screenshot_png: bytes,
    *,
    image_format: ImageFormat = "jpeg",
    image_quality: int = 60,
) -> tuple[bytes, int, int]:
    """Encode a screenshot without changing the device resolution."""

    with Image.open(BytesIO(screenshot_png)) as source:
        image = source.copy()
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
