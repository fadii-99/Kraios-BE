"""Image normalization copied from the developer Gemini pipeline."""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass

import fitz
from PIL import Image

from app.ai.config import ai_settings

_PDF_MAGIC = b"%PDF-"


@dataclass
class PreparedImage:
    png_bytes: bytes
    data_url: str  # "data:image/png;base64,...."


def _is_pdf(data: bytes) -> bool:
    return data[:5] == _PDF_MAGIC


def _pdf_first_page_to_png(data: bytes) -> bytes:
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        if doc.page_count == 0:
            raise ValueError("PDF has no pages")
        page = doc.load_page(0)
        # zoom = dpi / 72 (PDF default is 72 dpi)
        zoom = ai_settings.PDF_RENDER_DPI / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


def _normalize_raster(data: bytes) -> bytes:
    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    longest = max(img.size)
    if longest > ai_settings.MAX_IMAGE_DIM:
        scale = ai_settings.MAX_IMAGE_DIM / longest
        new_size = (round(img.size[0] * scale), round(img.size[1] * scale))
        img = img.resize(new_size, Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def prepare_image(raw: bytes) -> PreparedImage:
    """Detect type, rasterize PDFs, downscale large images, return PNG + data URL."""
    if not raw:
        raise ValueError("empty file")
    if len(raw) > ai_settings.MAX_UPLOAD_BYTES:
        raise ValueError("file too large")

    if _is_pdf(raw):
        png = _pdf_first_page_to_png(raw)
        png = _normalize_raster(png)  # also caps the rasterized page size
    else:
        try:
            png = _normalize_raster(raw)
        except Exception as exc:  # noqa: BLE001 - surface a clean message upstream
            raise ValueError(f"unsupported or corrupt image: {exc}") from exc

    b64 = base64.b64encode(png).decode("ascii")
    return PreparedImage(png_bytes=png, data_url=f"data:image/png;base64,{b64}")
