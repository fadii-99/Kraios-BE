"""Style-reference ordering helpers for the guided Gemini Step 2 pipeline."""
from __future__ import annotations

import mimetypes
from pathlib import Path

from app.ai.config import ai_settings


def guess_mime(path: str | Path) -> str:
    """Media type of an image, sniffed from its content rather than its name.

    The filename lies: the render pipeline writes JPEG bytes under .png names, and
    a declared type that disagrees with the bytes gets the request rejected
    ("specified using the image/png media type, but the image appears to be a
    image/jpeg image"). Falls back to the extension only if the bytes are
    unreadable.
    """
    try:
        from PIL import Image

        Image.init()
        with Image.open(path) as img:
            fmt = (img.format or "").upper()
        if fmt == "MPO":  # multi-picture JPEG — plain JPEG as far as any API cares
            fmt = "JPEG"
        mime = Image.MIME.get(fmt)
        if mime:
            return mime
    except Exception as exc:
        print(f"Could not identify image format for {path}, falling back to extension: {exc}")

    mime, _ = mimetypes.guess_type(str(path))
    return mime or "image/png"


def bundled_sketchup_reference(mode: str) -> tuple[bytes, str] | None:
    """Return the bundled SketchUp style reference, when configured and present."""
    if mode != "sketchup":
        return None
    ref_path = ai_settings.SKETCHUP_STYLE_REF
    if ref_path.is_file():
        # SKETCHUP_STYLE_REF is env-configurable, so don't assume it is a PNG.
        return ref_path.read_bytes(), guess_mime(ref_path)
    return None


def asset_style_references(paths: list[str]) -> list[tuple[bytes, str]]:
    """Read user-provided style references in caller-provided order."""
    refs: list[tuple[bytes, str]] = []
    for path in paths:
        if not path:
            continue
        file_path = Path(path)
        if not file_path.is_file():
            continue
        refs.append((file_path.read_bytes(), guess_mime(file_path)))
    return refs
