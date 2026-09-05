"""A synchronous OpenRouter chat client for this app.

WHY NOT REUSE `app.ai.openrouter_floorplan_client`
--------------------------------------------------
That client is async, hard-wired to the floor-plan model and token budget, and
raises exception types that `app.ai` catches by identity. Importing it would
make `bim` un-deletable without touching `app`, and would couple this app's
retry behaviour to a module owned by Step 1. The cost of independence is the
hundred lines below; the benefit is that `rm -rf bim/` is a complete removal.

Synchronous on purpose: every caller is a Celery task or a management command,
both of which are already a thread of their own. `async` here would only mean
an `async_to_sync` wrapper at each call site.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from bim.ai.config import bim_ai_settings

logger = logging.getLogger(__name__)

# Retried on: OpenRouter's own capacity errors and upstream 5xx. A 4xx is a
# request we built wrong and will build wrong again, so it fails immediately.
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}

# Two retries, exponential from 2s. Beyond this a caller waiting on a
# generation has been waiting long enough that failing is the kinder answer.
_MAX_TRANSPORT_RETRIES = 2
_RETRY_BASE_SECONDS = 2.0


class BimModelError(RuntimeError):
    """The model call failed. The message is safe to log, not to return."""


class BimModelTruncated(BimModelError):
    """Output hit the token ceiling; whatever came back is incomplete.

    Distinguished from a malformed answer because the remedies differ: a
    truncated response needs a smaller request or a bigger budget, while a
    malformed one can be repaired by showing the model its own mistake.
    """


class BimModelRefused(BimModelError):
    """The model returned no content — a refusal, a filter, or an empty turn."""


@dataclass
class ModelReply:
    text: str
    model: str
    # Token counts when the provider reports them. Used for the cost line in
    # the extraction record; absent is normal, not an error.
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    latency_ms: int = 0


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {bim_ai_settings.API_KEY}",
        "HTTP-Referer": bim_ai_settings.SITE_URL,
        "X-Title": bim_ai_settings.APP_NAME,
        "Content-Type": "application/json",
    }


def complete_json(
    messages: List[dict],
    *,
    model: str,
    max_tokens: int,
    operation: str,
    temperature: float = 0.1,
) -> ModelReply:
    """Call chat/completions in JSON mode and return the assistant's text.

    `operation` is a short label for the logs so a survey call, a geometry call
    and a repair round are distinguishable in a worker's output rather than all
    reading the same.

    Raises `BimModelError` (or a subclass) on every failure path. Callers are
    expected to catch it — nothing here is recoverable by the transport layer
    beyond the transient retries below.
    """
    if not bim_ai_settings.is_configured():
        raise BimModelError(
            "OPENROUTER_API_KEY is not set; the BIM extraction pipeline is unavailable."
        )

    url = f"{bim_ai_settings.BASE_URL.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    started = time.monotonic()
    response = _post_with_retries(url, payload, operation=operation, model=model)
    latency_ms = int((time.monotonic() - started) * 1000)

    data = response.json()
    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise BimModelError(f"unexpected response shape from {model}") from exc

    content = message.get("content")
    finish_reason = choice.get("finish_reason") or choice.get("native_finish_reason")

    # Order matters: a truncated response often also has usable-looking
    # content, and treating it as valid is how a half-written plan reaches the
    # parser and produces a confusing schema error instead of a clear one.
    if finish_reason == "length":
        raise BimModelTruncated(
            f"{model} hit the {max_tokens}-token ceiling during {operation}; "
            "the JSON is incomplete."
        )

    if not content:
        refusal = message.get("refusal")
        raise BimModelRefused(
            f"{model} returned no content during {operation} "
            f"(finish_reason={finish_reason!r}, refusal={refusal!r})"
        )

    usage = data.get("usage") or {}
    reply = ModelReply(
        text=content,
        model=model,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        latency_ms=latency_ms,
    )
    logger.info(
        "bim.%s ok model=%s latency=%dms prompt_tokens=%s completion_tokens=%s",
        operation,
        model,
        latency_ms,
        reply.prompt_tokens,
        reply.completion_tokens,
    )
    return reply


def _post_with_retries(
    url: str, payload: dict, *, operation: str, model: str
) -> httpx.Response:
    last_detail = "no attempt was made"

    for attempt in range(_MAX_TRANSPORT_RETRIES + 1):
        try:
            with httpx.Client(timeout=bim_ai_settings.REQUEST_TIMEOUT) as client:
                response = client.post(url, headers=_headers(), json=payload)
        except httpx.HTTPError as exc:
            last_detail = f"network error: {exc}"
            logger.warning(
                "bim.%s transport failure (attempt %d/%d) model=%s: %s",
                operation,
                attempt + 1,
                _MAX_TRANSPORT_RETRIES + 1,
                model,
                exc,
            )
        else:
            if response.status_code == 200:
                return response
            # Response bodies from a provider can carry the prompt back; only
            # a short prefix is logged, and none of it reaches a client.
            last_detail = f"HTTP {response.status_code}: {response.text[:300]}"
            if response.status_code not in _RETRYABLE_STATUS:
                raise BimModelError(f"{model} rejected the {operation} call — {last_detail}")
            logger.warning(
                "bim.%s retryable status (attempt %d/%d) model=%s: %s",
                operation,
                attempt + 1,
                _MAX_TRANSPORT_RETRIES + 1,
                model,
                last_detail,
            )

        if attempt < _MAX_TRANSPORT_RETRIES:
            time.sleep(_RETRY_BASE_SECONDS * (2**attempt))

    raise BimModelError(f"{model} failed the {operation} call after retries — {last_detail}")


def vision_messages(
    system_prompt: str, user_text: str, image_data_urls: List[str]
) -> List[dict]:
    """An OpenAI-compatible message list carrying one or more images."""
    content: List[Dict[str, Any]] = [{"type": "text", "text": user_text}]
    for data_url in image_data_urls:
        content.append({"type": "image_url", "image_url": {"url": data_url}})
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]
