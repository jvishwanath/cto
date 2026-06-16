"""
Tesseract OCR wrapper. Local, no network. Degrades to a no-op (returns
"" with a reason) when pytesseract/Pillow or the tesseract binary
aren't installed, so callers can fall back to vision.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..config import OCR_ENABLED

log = logging.getLogger(__name__)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif", ".webp"}

_available: bool | None = None
_reason: str = ""


def ocr_available() -> tuple[bool, str]:
    """Cached probe: are pytesseract + Pillow + the tesseract binary
    all present?"""
    global _available, _reason
    if _available is not None:
        return _available, _reason
    if not OCR_ENABLED:
        _available, _reason = False, "OCR_ENABLED=false"
        return _available, _reason
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
        # Probe the binary — import succeeds even when tesseract isn't on PATH.
        pytesseract.get_tesseract_version()
        _available, _reason = True, ""
    except Exception as e:
        _available = False
        _reason = f"{type(e).__name__}: {e}"
        log.info("OCR unavailable (%s); install: pip install '.[ocr]' + "
                 "the tesseract binary", _reason)
    return _available, _reason


def _usable_chars(text: str) -> int:
    """Count alphanumeric chars — a rough 'is this real text?' signal
    that ignores OCR's punctuation noise on diagrams."""
    return len(re.findall(r"[A-Za-z0-9]", text))


def ocr_image(path: str | Path) -> tuple[str, int]:
    """Returns (text, usable_char_count). Empty + 0 when OCR can't run
    or the image yields nothing."""
    ok, why = ocr_available()
    if not ok:
        return "", 0
    try:
        import pytesseract
        from PIL import Image

        # Open, normalize mode, and re-encode to an in-memory PNG that
        # we hand to tesseract via a fresh PIL image. Passing the raw
        # path or some source modes can trip pytesseract's output
        # decoding ('utf-8 can't decode 0x89' = it mis-read PNG bytes).
        with Image.open(path) as im:
            img = im.convert("RGB")
        text = pytesseract.image_to_string(img)
    except Exception as e:
        log.warning("ocr_image(%s) failed: %s", path, e)
        return "", 0
    text = text.strip()
    return text, _usable_chars(text)
