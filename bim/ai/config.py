"""Model and tuning configuration for the BIM extraction pipeline.

Reads `os.environ` directly. `config/settings.py` calls `load_dotenv()` before
any app is imported, so the process environment is already populated; this
module deliberately does not load `.env` itself, does not import Django
settings, and does not import `app.ai.config`. That is what lets the whole
package be deleted without leaving a dangling reference.

WHY THREE MODELS
----------------
Extraction is split into a survey pass and a geometry pass (see
`extractor.py`), and audited by a third. They are separately configurable
because they are separately hard:

  survey    — reads scale, building type, storey count and the room list off
              the drawing. Small output, needs careful reading and arithmetic.
  geometry  — emits every wall, opening and room polygon. Large structured
              output; the model that is best at this is not necessarily the
              best reader.
  furniture — counts and places every desk, chair, wc and basin. Separated from
              geometry because the two compete: asked for both at once, a model
              spends its attention on the hundred repeating workstations and
              its coordinates on the walls get worse. It also lets furniture
              fail without costing the building.
  audit     — compares a text summary of the result against the drawing.
              Runs on every attempt, so it is the one place worth optimising
              for latency and cost.

DEFAULTS
--------
The geometry default matches what this repository already settled on for
floor-plan JSON extraction in `app/ai/config.py` — that choice was made against
real plans and there is no reason to re-litigate it here. The survey default is
a different provider on purpose: when two models disagree about a drawing's
scale, that disagreement shows up as a grader finding instead of as a
confidently wrong model. Every one of them is env-overridable, so switching is
a config change, not a code change.
"""
from __future__ import annotations

import os


def _flag(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class BimAISettings:
    # -- provider ---------------------------------------------------------
    # One OpenRouter key reaches every model below, which is why this app does
    # not carry per-provider credentials of its own.
    API_KEY = os.environ.get("OPENROUTER_API_KEY")
    BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    SITE_URL = os.environ.get("OPENROUTER_SITE_URL") or "http://localhost:8000"
    APP_NAME = os.environ.get("OPENROUTER_APP_NAME", "Kraios BIM Engine")

    # -- models -----------------------------------------------------------
    MODEL_SURVEY = os.environ.get("BIM_MODEL_SURVEY", "anthropic/claude-fable-5")
    MODEL_GEOMETRY = os.environ.get("BIM_MODEL_GEOMETRY", "openai/gpt-5.6-sol")
    MODEL_FURNITURE = os.environ.get("BIM_MODEL_FURNITURE", "openai/gpt-5.6-sol")
    MODEL_AUDIT = os.environ.get("BIM_MODEL_AUDIT", "anthropic/claude-haiku-4.5")

    # -- output budgets ---------------------------------------------------
    # The survey is a page of JSON at most. The geometry pass on a large
    # commercial sheet runs to 60+ walls with room polygons, and a response cut
    # off at the limit is never parseable — the whole vision call has to be
    # paid for again — so the geometry budget is deliberately generous.
    MAX_TOKENS_SURVEY = _int("BIM_MAX_TOKENS_SURVEY", 4_000)
    MAX_TOKENS_GEOMETRY = _int("BIM_MAX_TOKENS_GEOMETRY", 32_000)
    # An open-plan office can carry a hundred workstations, and each one is a
    # desk AND a chair. Under-budgeting this pass truncates the furniture list
    # in the middle, which is indistinguishable from a half-furnished drawing.
    MAX_TOKENS_FURNITURE = _int("BIM_MAX_TOKENS_FURNITURE", 24_000)
    MAX_TOKENS_AUDIT = _int("BIM_MAX_TOKENS_AUDIT", 2_000)

    REQUEST_TIMEOUT = _float("BIM_REQUEST_TIMEOUT", 300.0)

    # -- extraction loop --------------------------------------------------
    # Attempts at the GEOMETRY pass. The survey is not repeated: if the drawing
    # was misread, re-reading it with the same model and the same image gives
    # the same answer, and the grader's findings are about geometry anyway.
    MAX_GEOMETRY_ATTEMPTS = _int("BIM_MAX_GEOMETRY_ATTEMPTS", 3)
    # Repairs of malformed/unparseable output within one attempt. Distinct from
    # the above: this is "the JSON was broken", that is "the model was wrong".
    MAX_SCHEMA_REPAIRS = _int("BIM_MAX_SCHEMA_REPAIRS", 2)
    # A result at or above this is returned without another attempt.
    MIN_ACCEPT_SCORE = _int("BIM_MIN_ACCEPT_SCORE", 70)

    # -- furniture --------------------------------------------------------
    # Fails open, like the audit: a plan with no furniture is still a building.
    FURNITURE_ENABLED = _flag("BIM_FURNITURE_ENABLED", True)
    # A plan claiming more than this has almost certainly started emitting one
    # fixture per drawn line. Kept generous — a large office floor really does
    # have hundreds.
    MAX_FIXTURES = _int("BIM_MAX_FIXTURES", 600)

    # -- visual audit -----------------------------------------------------
    # Fails open: if the auditor errors or is disabled, extraction proceeds on
    # the geometry score alone. A QA gate that can block a working pipeline is
    # worse than no gate.
    AUDIT_ENABLED = _flag("BIM_AUDIT_ENABLED", True)

    # -- image preparation ------------------------------------------------
    # Above this the extra pixels stop buying accuracy and start costing
    # tokens; large architectural sheets are the normal input here, so this is
    # set higher than a general-purpose vision default would be.
    MAX_IMAGE_DIM = _int("BIM_MAX_IMAGE_DIM", 2000)
    MAX_UPLOAD_BYTES = _int("BIM_MAX_UPLOAD_BYTES", 25 * 1024 * 1024)
    PDF_RENDER_DPI = _int("BIM_PDF_RENDER_DPI", 200)

    @classmethod
    def is_configured(cls) -> bool:
        return bool(cls.API_KEY)


bim_ai_settings = BimAISettings()
