"""Parse JSON out of a model response that was supposed to be only JSON.

Every model is asked for a bare JSON object and most comply. The ones that do
not fail in a small, well-known set of ways, and each is cheaper to tolerate
here than to spend a repair round on:

  - wrapping the object in ```json fences despite being told not to;
  - prefacing it with a sentence of explanation;
  - leaving a trailing comma before a closing brace or bracket.

Anything beyond those raises, and the caller shows the model its own error.
"""
from __future__ import annotations

import json
import re

_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def parse_object(text: str) -> dict:
    """Return the JSON object in `text`. Raises ValueError if there is none."""
    if not text or not text.strip():
        raise ValueError("the model returned an empty response")

    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        if len(parts) >= 2:
            cleaned = parts[1]
            if cleaned.lstrip().lower().startswith("json"):
                cleaned = cleaned.lstrip()[4:]
        cleaned = cleaned.strip()

    for candidate in (cleaned, _outermost_object(cleaned)):
        if candidate is None:
            continue
        parsed = _loads_lenient(candidate)
        if parsed is not None:
            if not isinstance(parsed, dict):
                raise ValueError(
                    f"expected a JSON object, got {type(parsed).__name__}"
                )
            return parsed

    raise ValueError("the response did not contain a parseable JSON object")


def _loads_lenient(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_TRAILING_COMMA.sub(r"\1", text))
    except json.JSONDecodeError:
        return None


def _outermost_object(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    return text[start : end + 1]


def json_dumps(value) -> str:
    """Compact-but-readable JSON for embedding in a prompt.

    `ensure_ascii=False` so a room labelled in Urdu or Arabic reaches the model
    as that text rather than as escape sequences it has to decode.
    """
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)
