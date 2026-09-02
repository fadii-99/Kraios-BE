"""Gemini image model client copied from the developer guided render pipeline.

The guided ("sketchout") render is routed through OpenRouter
(https://openrouter.ai/google/gemini-3.1-pro-preview) when an OpenRouter key is
configured; otherwise it falls back to the Google GenerativeLanguage API.
"""
from __future__ import annotations

import base64
import re

import httpx

from app.ai.config import ai_settings
from app.ai.llm_call_logging import log_llm_call


class GeminiError(RuntimeError):
    """User-facing failure talking to Gemini (config, network, or bad output)."""


def _img_part(data: bytes, mime: str) -> dict:
    return {
        "inlineData": {
            "mimeType": mime,
            "data": base64.b64encode(data).decode("ascii"),
        }
    }


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


async def photorealize(
    image_bytes: bytes,
    mime_type: str,
    prompt: str,
    extra_images: list[tuple[bytes, str]] | None = None,
) -> bytes:
    """Send the snapshot + ordered extra images + prompt to the image model;
    return the generated image bytes.

    Order matters and must match the prompt's INPUTS_PREAMBLE:
    IMAGE 1 = the snapshot (`image_bytes`); then `extra_images` in order
    (typically the original 2D CAD plan, then any style references).

    Prefers OpenRouter (google/gemini-3.1-pro-preview); falls back to the
    Google GenerativeLanguage API when OpenRouter is unavailable or fails.
    """
    if ai_settings.OPENROUTER_API_KEY:
        try:
            with log_llm_call(
                provider="OpenRouter",
                model=ai_settings.OPENROUTER_GUIDED_RENDER_MODEL,
                api=f"{ai_settings.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
                operation="guided image generation",
            ):
                return await _photorealize_openrouter(
                    image_bytes, mime_type, prompt, extra_images
                )
        except Exception:
            if not ai_settings.GEMINI_API_KEY:
                raise
    if ai_settings.GEMINI_API_KEY:
        with log_llm_call(
            provider="Google Gemini API",
            model=ai_settings.GEMINI_IMAGE_MODEL,
            api=(
                f"{ai_settings.GEMINI_BASE_URL.rstrip('/')}/models/"
                f"{ai_settings.GEMINI_IMAGE_MODEL}:generateContent"
            ),
            operation="guided image generation fallback",
        ):
            return await _photorealize_google(
                image_bytes, mime_type, prompt, extra_images
            )
    raise GeminiError(
        "No render provider configured: set OPENROUTER_API_KEY (preferred) or "
        "GEMINI_API_KEY/GOOGLE_API_KEY."
    )


async def _photorealize_openrouter(
    image_bytes: bytes,
    mime_type: str,
    prompt: str,
    extra_images: list[tuple[bytes, str]] | None = None,
) -> bytes:
    url = f"{ai_settings.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"

    content: list[dict] = [{"type": "text", "text": prompt}]
    content.append(
        {"type": "image_url", "image_url": {"url": _data_url(image_bytes, mime_type)}}
    )
    for ex_bytes, ex_mime in extra_images or []:
        content.append(
            {"type": "image_url", "image_url": {"url": _data_url(ex_bytes, ex_mime)}}
        )

    payload = {
        "model": ai_settings.OPENROUTER_GUIDED_RENDER_MODEL,
        "messages": [{"role": "user", "content": content}],
        "modalities": ["image", "text"],
    }
    headers = {
        "Authorization": f"Bearer {ai_settings.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    if ai_settings.OPENROUTER_SITE_URL:
        headers["HTTP-Referer"] = ai_settings.OPENROUTER_SITE_URL
    if ai_settings.OPENROUTER_APP_NAME:
        headers["X-Title"] = ai_settings.OPENROUTER_APP_NAME

    try:
        async with httpx.AsyncClient(timeout=ai_settings.GEMINI_REQUEST_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise GeminiError(f"network error calling OpenRouter: {exc}") from exc

    if resp.status_code != 200:
        raise GeminiError(f"OpenRouter returned {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GeminiError(f"unexpected OpenRouter response shape: {data}") from exc

    img = _extract_openrouter_image(message)
    if img is not None:
        return img

    text = message.get("content")
    if isinstance(text, list):
        text = " ".join(p.get("text", "") for p in text if isinstance(p, dict))
    raise GeminiError(
        "OpenRouter did not return an image. "
        + (f"Model said: {str(text)[:300]}" if text else "Empty response.")
    )


def _decode_image_url(value: str) -> bytes | None:
    if value.startswith("data:image/"):
        return base64.b64decode(value.split(",", 1)[1])
    return None


def _extract_openrouter_image(message: dict) -> bytes | None:
    # Preferred: OpenAI-style image output array on the message.
    for entry in message.get("images") or []:
        if isinstance(entry, dict):
            iu = entry.get("image_url")
            url = iu.get("url") if isinstance(iu, dict) else iu
            if isinstance(url, str):
                decoded = _decode_image_url(url)
                if decoded is not None:
                    return decoded

    # Fallback: image parts embedded in the content array.
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            iu = part.get("image_url")
            url = iu.get("url") if isinstance(iu, dict) else iu
            if isinstance(url, str):
                decoded = _decode_image_url(url)
                if decoded is not None:
                    return decoded

    # Last resort: a data URL embedded in text content.
    text = content if isinstance(content, str) else None
    if text:
        m = re.search(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+", text)
        if m:
            return base64.b64decode(m.group(0).split(",", 1)[1])
    return None


async def _photorealize_google(
    image_bytes: bytes,
    mime_type: str,
    prompt: str,
    extra_images: list[tuple[bytes, str]] | None = None,
) -> bytes:
    url = (
        f"{ai_settings.GEMINI_BASE_URL.rstrip('/')}"
        f"/models/{ai_settings.GEMINI_IMAGE_MODEL}:generateContent"
    )
    parts: list[dict] = [{"text": prompt}, _img_part(image_bytes, mime_type)]
    for ex_bytes, ex_mime in extra_images or []:
        parts.append(_img_part(ex_bytes, ex_mime))
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    try:
        async with httpx.AsyncClient(timeout=ai_settings.GEMINI_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                url,
                params={"key": ai_settings.GEMINI_API_KEY},
                json=payload,
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise GeminiError(f"network error calling Gemini: {exc}") from exc

    if resp.status_code != 200:
        raise GeminiError(
            f"Gemini returned {resp.status_code}: {resp.text[:500]}"
        )

    data = resp.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GeminiError(f"unexpected Gemini response shape: {data}") from exc

    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"])

    # No image part: surface any text the model returned (often a refusal/reason).
    text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
    raise GeminiError(
        "Gemini did not return an image. "
        + (f"Model said: {text[:300]}" if text else "Empty response.")
    )
