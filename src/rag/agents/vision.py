"""
Read an image attachment into text for use as query context.

Strategy (ephemeral — never indexed):
  1. Tesseract OCR fast-path (local, free). Good for screenshots of
     code, logs, stack traces, error dialogs.
  2. If OCR yields too few usable chars (likely a diagram/architecture
     sketch, not text) AND VISION_FALLBACK is on, send the image to a
     vision model via the LiteLLM gateway to DESCRIBE/transcribe it.

Returns a markdown block the caller prepends to the user's question.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path

from langchain_core.messages import HumanMessage

from ..config import VISION_FALLBACK, OCR_MIN_CHARS
from ..ingest.ocr import ocr_image, ocr_available, IMAGE_EXTS
from .llm import llm

_TEXT_EXTS = {".txt", ".md", ".markdown", ".log", ".json", ".yaml", ".yml",
              ".py", ".java", ".go", ".js", ".ts", ".sh", ".sql", ".xml",
              ".toml", ".ini", ".cfg", ".conf", ".csv", ".diff", ".patch"}
_TEXT_CAP = 50_000

log = logging.getLogger(__name__)

_VISION_PROMPT = (
    "Transcribe and describe this image for a code/infrastructure "
    "assistant. If it contains text (code, logs, an error, a config), "
    "transcribe it verbatim in a fenced block. If it's a diagram or "
    "architecture sketch, describe the components and their "
    "relationships. Be concise and factual — no preamble."
)

_MAX_BYTES = 8 * 1024 * 1024  # 8 MB guard for the gateway payload


def _data_url(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except Exception:
        return None
    if len(raw) > _MAX_BYTES:
        log.warning("image %s too large (%d bytes); skipping vision",
                    path, len(raw))
        return None
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    b64 = base64.b64encode(raw).decode()
    return f"data:{mime};base64,{b64}"


def _vision_describe(path: Path) -> str:
    url = _data_url(path)
    if not url:
        return ""
    try:
        model = llm("vision", streaming=False, temperature=0)
        resp = model.invoke([HumanMessage(content=[
            {"type": "text", "text": _VISION_PROMPT},
            {"type": "image_url", "image_url": {"url": url}},
        ])])
        return (resp.content if isinstance(resp.content, str)
                else str(resp.content)).strip()
    except Exception as e:
        log.warning("vision describe(%s) failed: %s", path, e)
        return ""


def read_image(path: str | Path) -> dict:
    """Extract text/description from an image.

    Returns {"text": str, "method": "ocr"|"vision"|"none",
             "chars": int, "name": str}. `text` is "" when nothing
    could be read.
    """
    path = Path(path)
    name = path.name

    text, chars = ocr_image(path)
    method = "ocr" if chars else "none"

    # Escalate to vision when OCR was thin (diagram, not a text shot).
    if chars < OCR_MIN_CHARS and VISION_FALLBACK:
        desc = _vision_describe(path)
        if desc:
            # Prefer vision's richer output, but keep any OCR text it
            # might have missed by appending it when both exist.
            text = desc
            method = "vision"

    return {"text": text, "method": method, "chars": chars, "name": name}


def read_attachment(path: str | Path) -> dict:
    """Dispatch by extension: images → read_image() (OCR/vision);
    text-ish files → read_text capped at _TEXT_CAP. Returns the same
    {text, method, name} shape so format_attachment() works for both."""
    p = Path(path)
    ext = p.suffix.lower()
    name = p.name
    if ext in IMAGE_EXTS:
        return read_image(p)
    if ext in _TEXT_EXTS or ext == "":
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"text": "", "method": "none", "name": name,
                    "chars": 0, "error": str(e)}
        truncated = len(raw) > _TEXT_CAP
        text = raw[:_TEXT_CAP]
        return {"text": text, "method": "file",
                "name": name, "chars": len(text),
                "truncated": truncated}
    return {"text": "", "method": "none", "name": name, "chars": 0,
            "error": f"unsupported type {ext!r}"}


def read_attachments(paths: list) -> tuple[str, list[dict]]:
    """Read every path; return (combined markdown block, per-file
    metadata list). Skips empties."""
    blocks: list[str] = []
    meta: list[dict] = []
    for p in paths or []:
        r = read_attachment(p)
        meta.append(r)
        blocks.append(format_attachment(r))
    return "".join(blocks), meta


def format_attachment(result: dict) -> str:
    """Render read_image() output as a markdown block to prepend to
    the user's query. Empty string when nothing was extracted."""
    if not result.get("text"):
        ok, why = ocr_available()
        hint = "" if (ok or VISION_FALLBACK) else f" (OCR unavailable: {why})"
        return (f"_[attached image `{result.get('name','image')}` — "
                f"no text could be extracted{hint}]_\n\n")
    method = result.get("method")
    if method == "file":
        trunc = (f" (truncated to {_TEXT_CAP:,} chars)"
                 if result.get("truncated") else "")
        return (f"[Attached file `{result['name']}`{trunc}]:\n"
                f"```\n{result['text']}\n```\n\n")
    label = ("transcribed via OCR" if method == "ocr"
             else "read via vision model")
    return (f"[Attached image `{result['name']}` — {label}]:\n"
            f"```\n{result['text']}\n```\n\n")
