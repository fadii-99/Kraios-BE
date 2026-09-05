"""Turn whatever the user uploaded into a PNG a vision model can read.

Accepts the formats architects actually send: PNG, JPEG, WebP, TIFF, BMP, and
single- or multi-page PDF. Everything becomes RGB PNG bytes plus a data URL.

TWO DECISIONS WORTH KNOWING ABOUT
---------------------------------
1. Transparency is flattened onto WHITE, not black. A PNG exported from CAD is
   very often line-work on transparency; composited onto the default black it
   becomes black-on-black and the model reports an empty drawing.

2. Downscaling is capped by the LONGEST edge and never upscales. Architectural
   sheets are wide; scaling by area or by width alone squeezes a 4000x900
   elevation into unreadability. Small images are left alone because
   interpolating a 600px scan does not add detail, it adds artefacts.

Uses Pillow and PyMuPDF, both already project dependencies. Nothing here
imports Django.
"""
from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from typing import List

from PIL import Image, ImageOps, UnidentifiedImageError

from bim.ai.config import bim_ai_settings

logger = logging.getLogger(__name__)

# Pillow refuses very large images as a decompression-bomb guard. Architectural
# scans legitimately exceed the default, so the ceiling is raised — but kept
# finite, because "no limit" turns a malicious upload into an OOM.
Image.MAX_IMAGE_PIXELS = 300_000_000

# A PDF beyond this many pages is a drawing set, not a floor plan. Only the
# first page is read either way; the count exists to fail with a clear message
# rather than to silently ignore 40 sheets.
MAX_PDF_PAGES = 40

_PDF_MAGIC = b"%PDF-"


class ImagePreparationError(ValueError):
    """The upload could not be turned into a readable image.

    A `ValueError` because every cause is bad input, and callers map it to a
    400 rather than a 500.
    """


@dataclass
class PreparedImage:
    png_bytes: bytes
    width: int
    height: int
    source_kind: str  # "image" | "pdf"
    page_count: int = 1

    @property
    def data_url(self) -> str:
        encoded = base64.b64encode(self.png_bytes).decode("ascii")
        return f"data:image/png;base64,{encoded}"


def prepare(raw: bytes, *, filename: str = "") -> PreparedImage:
    """Normalise one uploaded file. Raises `ImagePreparationError` on bad input."""
    if not raw:
        raise ImagePreparationError("The uploaded file is empty.")
    if len(raw) > bim_ai_settings.MAX_UPLOAD_BYTES:
        limit_mb = bim_ai_settings.MAX_UPLOAD_BYTES / (1024 * 1024)
        raise ImagePreparationError(f"The file is larger than the {limit_mb:.0f} MB limit.")

    if raw[:5] == _PDF_MAGIC:
        return _from_pdf(raw)
    return _from_image(raw, filename=filename)


def _from_image(raw: bytes, *, filename: str) -> PreparedImage:
    try:
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            # Phone photographs of a printed plan carry an EXIF rotation that
            # every viewer honours and PIL does not, so a sideways drawing
            # reaches the model unless it is applied here.
            source = ImageOps.exif_transpose(source)
            flattened = _flatten_to_rgb(source)
    except UnidentifiedImageError as exc:
        hint = f" ({filename})" if filename else ""
        raise ImagePreparationError(
            f"The file{hint} is not an image or PDF this system can read."
        ) from exc
    except OSError as exc:  # truncated / corrupt file
        raise ImagePreparationError("The image file is damaged or incomplete.") from exc

    resized = _downscale(flattened)
    return PreparedImage(
        png_bytes=_to_png(resized),
        width=resized.width,
        height=resized.height,
        source_kind="image",
    )


def _from_pdf(raw: bytes) -> PreparedImage:
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ImagePreparationError("PDF support is unavailable on this server.") from exc

    try:
        document = fitz.open(stream=raw, filetype="pdf")
    except Exception as exc:  # PyMuPDF raises bare exceptions for bad files
        raise ImagePreparationError("The PDF could not be opened.") from exc

    with document:
        page_count = document.page_count
        if page_count == 0:
            raise ImagePreparationError("The PDF has no pages.")
        if page_count > MAX_PDF_PAGES:
            raise ImagePreparationError(
                f"The PDF has {page_count} pages. Upload the single sheet holding the "
                "floor plan."
            )
        # Only the first page. A caller that needs sheet 3 should send sheet 3;
        # guessing which page of a set is "the plan" is a decision this layer
        # has no basis to make.
        page = document.load_page(0)
        zoom = bim_ai_settings.PDF_RENDER_DPI / 72.0
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)

    resized = _downscale(image)
    return PreparedImage(
        png_bytes=_to_png(resized),
        width=resized.width,
        height=resized.height,
        source_kind="pdf",
        page_count=page_count,
    )


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    """Composite any alpha channel onto white. See the module docstring."""
    if image.mode == "RGB":
        return image
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(canvas, rgba).convert("RGB")
    return image.convert("RGB")


def _downscale(image: Image.Image) -> Image.Image:
    longest = max(image.width, image.height)
    limit = bim_ai_settings.MAX_IMAGE_DIM
    if longest <= limit:
        return image
    ratio = limit / longest
    size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
    logger.info(
        "bim.imaging downscaled %dx%d -> %dx%d", image.width, image.height, *size
    )
    return image.resize(size, Image.Resampling.LANCZOS)


def _to_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    # `optimize` costs a few hundred milliseconds and saves enough bytes on
    # line-art (which compresses extremely well) to be worth it on every upload
    # that then crosses the network to a model provider.
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def data_urls(images: List[PreparedImage]) -> List[str]:
    return [image.data_url for image in images]
