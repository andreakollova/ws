"""
Pillow-based Instagram image composer.
Composites the event photo behind the Woeva template PNG overlay
(logo + gradient — template.png provided by you).
"""

import io
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

TEMPLATE_PATH = Path(__file__).parent / "template.png"


def create_event_image(photo_bytes: bytes | None = None) -> bytes:
    """
    Composite event photo under the Woeva PNG template overlay.

    Layout:
      - Event photo: stretched/cropped to fill template size (background)
      - template.png (RGBA with transparency): composited on top
        Contains: Woeva logo + gradient overlay

    Falls back to a dark background if no photo is provided.
    Returns JPEG bytes ready for Instagram upload.
    """
    from PIL import Image

    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(
            f"Template not found: {TEMPLATE_PATH}\n"
            "Place your template.png (logo + gradient, RGBA/PNG) in the bot/ directory."
        )

    template = Image.open(TEMPLATE_PATH).convert("RGBA")
    tw, th = template.size

    photo = result = None
    try:
        if photo_bytes:
            try:
                photo = Image.open(io.BytesIO(photo_bytes)).convert("RGBA")
                # Center-crop photo to match template aspect ratio
                photo_ratio = photo.width / photo.height
                tmpl_ratio = tw / th
                if photo_ratio > tmpl_ratio:
                    # Photo is wider — crop sides
                    new_w = int(photo.height * tmpl_ratio)
                    offset = (photo.width - new_w) // 2
                    photo = photo.crop((offset, 0, offset + new_w, photo.height))
                else:
                    # Photo is taller — crop top/bottom
                    new_h = int(photo.width / tmpl_ratio)
                    offset = (photo.height - new_h) // 2
                    photo = photo.crop((0, offset, photo.width, offset + new_h))
                photo = photo.resize((tw, th), Image.LANCZOS)
            except Exception as exc:
                logger.warning(f"Could not process photo: {exc}")
                if photo:
                    photo.close()
                photo = Image.new("RGBA", (tw, th), (10, 10, 10, 255))
        else:
            # Dark background fallback
            photo = Image.new("RGBA", (tw, th), (10, 10, 10, 255))

        result = Image.alpha_composite(photo, template)
        buf = io.BytesIO()
        result.convert("RGB").save(buf, format="JPEG", quality=95)
        return buf.getvalue()

    finally:
        for img in (photo, result, template):
            if img:
                try:
                    img.close()
                except Exception:
                    pass
