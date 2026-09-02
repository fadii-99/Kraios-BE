"""Async OpenRouter client: send image + instructions, get JSON text back."""
from __future__ import annotations

from typing import List

import httpx

from app.ai.config import ai_settings
from app.ai.llm_call_logging import log_llm_call


class OpenRouterError(RuntimeError):
    pass


class OpenRouterTruncatedError(OpenRouterError):
    """The model hit max_tokens mid-answer; the returned text is incomplete."""


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {ai_settings.OPENROUTER_API_KEY}",
        "HTTP-Referer": ai_settings.OPENROUTER_SITE_URL or "http://localhost:8000",
        "X-Title": ai_settings.OPENROUTER_APP_NAME,
        "Content-Type": "application/json",
    }


async def complete(
    messages: List[dict],
    operation: str = "floor-plan analysis",
    model: str | None = None,
) -> str:
    """Call chat/completions and return the assistant message content (string).

    `operation` is a human label shown in the terminal LLM log so distinct call
    types (extraction / repair / fidelity-audit / render-verify) are
    distinguishable instead of all reading "floor-plan analysis".
    `model` overrides the default floorplan model for this one call (used by
    the fast render-verify judge).
    """
    if not ai_settings.OPENROUTER_API_KEY:
        raise OpenRouterError("OPENROUTER_API_KEY is not set; floorplan analysis is unavailable.")
    url = f"{ai_settings.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
    model = model or ai_settings.OPENROUTER_MODEL_FLOORPLAN
    payload = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": ai_settings.OPENROUTER_FLOORPLAN_MAX_TOKENS,
    }
    try:
        with log_llm_call(
            provider="OpenRouter",
            model=model,
            api=url,
            operation=operation,
        ):
            async with httpx.AsyncClient(timeout=ai_settings.GEMINI_REQUEST_TIMEOUT) as client:
                resp = await client.post(url, headers=_headers(), json=payload)
            if resp.status_code != 200:
                raise OpenRouterError(
                    f"OpenRouter returned {resp.status_code}: {resp.text[:500]}"
                )
    except httpx.HTTPError as exc:
        raise OpenRouterError(f"network error calling OpenRouter: {exc}") from exc

    data = resp.json()
    try:
        choice = data["choices"][0]
        message = choice["message"]
        content = message.get("content")
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError(f"unexpected OpenRouter response shape: {data}") from exc

    # Some models (reasoning models, refusals, content filtering) return a
    # message with content=None. Surface a clear, diagnosable error instead of
    # letting a None leak downstream into `.strip()` (AttributeError).
    if not content:
        finish_reason = choice.get("finish_reason") or choice.get("native_finish_reason")
        refusal = message.get("refusal")
        detail = f"finish_reason={finish_reason!r}"
        if refusal:
            detail += f", refusal={refusal!r}"
        raise OpenRouterError(f"model returned empty content ({detail})")

    # A response cut off at max_tokens is NEVER valid JSON. Flag it explicitly
    # so callers can distinguish truncation from a malformed answer instead of
    # burning repair rounds on output that can only ever arrive incomplete.
    finish_reason = choice.get("finish_reason") or choice.get("native_finish_reason")
    if finish_reason == "length":
        raise OpenRouterTruncatedError(
            "model output was truncated at the max_tokens limit "
            f"({ai_settings.OPENROUTER_FLOORPLAN_MAX_TOKENS}); the JSON is incomplete. "
            "Respond more concisely (especially the 'description' field)."
        )

    return content


def build_messages(system_prompt: str, user_text: str, image_data_url: str) -> List[dict]:
    """OpenAI-compatible message list with a vision (image_url) content part."""
    return build_messages_multi(system_prompt, user_text, [image_data_url])


def build_messages_multi(
    system_prompt: str, user_text: str, image_data_urls: List[str]
) -> List[dict]:
    """Like build_messages but with one or more images, in the given order."""
    content = [{"type": "text", "text": user_text}]
    for url in image_data_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]
