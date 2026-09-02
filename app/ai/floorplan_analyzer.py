"""Orchestrate: uploaded bytes -> image -> model -> validated FloorPlan.

The model call is wrapped in a bounded, self-healing loop so it returns a
schema-valid FloorPlan regardless of which model is configured: a strong model
succeeds on the first attempt; a weaker one is fed its own parse/validation
error and asked to repair, repeated up to MAX_ATTEMPTS times.
"""
from __future__ import annotations

import base64
import json
import re

from pydantic import ValidationError

from app.ai import openrouter_floorplan_client as openrouter
from app.ai import prompts as prompts
from app.ai import verification
from app.ai.config import ai_settings
from app.ai.floorplan_schema import FloorPlan
from app.ai.image_preparation_service import prepare_image

# Total model calls per request: the initial attempt plus repair rounds.
MAX_ATTEMPTS = 3

# Errors that mean "the model produced bad/empty output" — all recoverable by
# feeding the error back and asking for a repair.
_RECOVERABLE = (ValidationError, json.JSONDecodeError, ValueError, openrouter.OpenRouterError)


async def _complete_and_validate(
    base_messages: list, fail_msg: str, operation: str = "floorplan-extract"
) -> FloorPlan:
    """Call the model and validate against FloorPlan, self-repairing on failure.

    On each failed attempt the raw output (if any) and the exact error are fed
    back to the model as a repair instruction, then we try again — up to
    MAX_ATTEMPTS total. Works with any model: strong models pass immediately,
    weak models converge over the repair rounds.

    Each internal retry is logged as "{operation} (schema-retry N)" so a JSON /
    schema self-heal is visibly distinct from the higher-level fidelity loop.
    """
    messages = base_messages
    last_raw = ""
    last_err: Exception | None = None

    for attempt in range(MAX_ATTEMPTS):
        op = operation if attempt == 0 else f"{operation} (schema-retry {attempt})"
        try:
            last_raw = await openrouter.complete(messages, operation=op)
            return FloorPlan.model_validate(_extract_json(last_raw))
        except _RECOVERABLE as err:
            last_err = err
            # Surface WHY the output was rejected — otherwise schema-retries
            # are indistinguishable in the terminal and unexplainable later.
            print(f"⚠️  {op} rejected: {type(err).__name__}: {str(err)[:300]}")
            # And WHAT was rejected: head/tail of the raw output (whitespace
            # made visible) so malformed shapes are diagnosable from the log.
            if last_raw:
                head = repr(last_raw[:160])
                tail = repr(last_raw[-160:]) if len(last_raw) > 320 else ""
                print(f"    raw head: {head}" + (f"\n    raw tail: {tail}" if tail else ""))
            # Truncated output is incomplete by definition — echoing it back
            # only inflates the next request without helping the repair.
            truncated = isinstance(err, openrouter.OpenRouterTruncatedError)
            previous = "" if truncated else (last_raw or "")[:6000]
            repair_text = prompts.REPAIR_PROMPT_TEMPLATE.format(
                error=str(err), previous=previous
            )
            # Build the next turn from the ORIGINAL prompt each time so the image
            # and instructions are always present; append the bad output (when we
            # have one) plus the repair request.
            messages = list(base_messages)
            if previous:
                messages.append({"role": "assistant", "content": last_raw})
            messages.append({"role": "user", "content": repair_text})

    raise AnalyzeError(f"{fail_msg} Last error: {last_err}") from last_err


def _data_url(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


class AnalyzeError(RuntimeError):
    """Raised when we cannot produce a valid FloorPlan; message is user-facing."""


def _loads_lenient(text: str) -> dict:
    """json.loads, but retry once after removing trailing commas — a very common
    model mistake (`{"a": 1,}` / `[1, 2,]`) that json.loads rejects."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        cleaned = re.sub(r",\s*([}\]])", r"\1", text)
        return json.loads(cleaned)


def _extract_json(text: str) -> dict:
    """Parse the model's JSON. Tolerate stray code fences / surrounding prose."""
    if not text or not text.strip():
        raise ValueError("empty model response")
    text = text.strip()
    if text.startswith("```"):
        # strip ```json ... ``` fences if the model added them despite instructions
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip().rstrip("`").strip()
    try:
        return _loads_lenient(text)
    except json.JSONDecodeError:
        # last resort: grab the outermost {...}
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return _loads_lenient(text[start : end + 1])
        raise


async def analyze_floorplan(raw: bytes) -> FloorPlan:
    img = prepare_image(raw)  # raises ValueError on bad input

    messages = openrouter.build_messages(
        prompts.SYSTEM_PROMPT, prompts.USER_PROMPT, img.data_url
    )
    return await _complete_and_validate(
        messages,
        "The model could not produce a valid floor plan for this image.",
        operation="floorplan-extract",
    )


# Repair instruction used by the Gate-1 fidelity loop. This is a QA/repair
# prompt, distinct from the extraction/design prompts in prompts.py.
_REPAIR_AGAINST_PLAN = (
    "A QA check compared geometry extracted from the attached 2D floor plan "
    "(IMAGE 1) against that plan and found these fidelity problems:\n"
    "{issues}\n\n"
    "Current extracted JSON:\n{current}\n\n"
    "Re-examine IMAGE 1 carefully and return a CORRECTED FloorPlan JSON that "
    "fixes every listed problem while staying faithful to the plan. Use the same "
    "schema and the same coordinate system (origin bottom-left, +x right, +y up, "
    "meters). Reproduce every wall, room, door and window in its true position. "
    "Return ONLY the JSON."
)


async def repair_floorplan_against_plan(
    plan_data_url: str, current_json: str, issues: list[str]
) -> FloorPlan:
    """Ask the model to fix a low-fidelity FloorPlan, shown the plan + issues."""
    issue_text = "\n".join(f"- {i}" for i in issues) or "- (unspecified)"
    user_text = _REPAIR_AGAINST_PLAN.format(
        issues=issue_text, current=(current_json or "")[:6000]
    )
    messages = openrouter.build_messages(prompts.SYSTEM_PROMPT, user_text, plan_data_url)
    return await _complete_and_validate(
        messages,
        "The model could not repair the floor plan against the 2D plan.",
        operation="floorplan-repair (gate1)",
    )


async def analyze_floorplan_verified(raw: bytes) -> tuple[FloorPlan, verification.FidelityReport | None]:
    """Extract the FloorPlan and run the Gate-1 fidelity loop.

    Returns the best-scoring plan plus its fidelity report. Fails open: if the
    gate is disabled or the judge errors, the plain extraction is returned with
    a passing/None report so generation is never blocked.
    """
    img = prepare_image(raw)
    plan = await analyze_floorplan(raw)

    if not ai_settings.FIDELITY_GATE_ENABLED:
        return plan, None

    best_plan = plan
    best_report = await verification.audit_floorplan(img.png_bytes, plan)

    attempts = max(1, ai_settings.FIDELITY_MAX_EXTRACTION_ATTEMPTS)
    tries = 1
    # Stop early once we pass; skip the loop entirely if the judge itself errored
    # (fail-open) since we have no trustworthy signal to correct against.
    while not best_report.passed and best_report.error is None and tries < attempts:
        tries += 1
        try:
            candidate = await repair_floorplan_against_plan(
                img.data_url, best_plan.model_dump_json(), best_report.issues
            )
        except Exception:
            try:
                candidate = await analyze_floorplan(raw)  # fresh re-extraction fallback
            except Exception:
                break
        report = await verification.audit_floorplan(img.png_bytes, candidate)
        if report.passed or report.score > best_report.score:
            best_plan, best_report = candidate, report
        if best_report.passed:
            break

    return best_plan, best_report


async def refine_floorplan(
    snapshot_png: bytes,
    render_png: bytes,
    cad_png: bytes | None,
    current_json: str,
) -> FloorPlan:
    """Feedback loop: correct the FloorPlan geometry by comparing the current
    3D snapshot against the CAD plan and the SketchUp render."""
    if cad_png:
        legend = prompts.REFINE_LEGEND_WITH_CAD
        images = [snapshot_png, cad_png, render_png]
    else:
        legend = prompts.REFINE_LEGEND_NO_CAD
        images = [snapshot_png, render_png]

    user_text = prompts.REFINE_USER_PROMPT_TEMPLATE.format(
        image_legend=legend, current_json=current_json[:8000]
    )
    messages = openrouter.build_messages_multi(
        prompts.SYSTEM_PROMPT, user_text, [_data_url(b) for b in images]
    )
    return await _complete_and_validate(
        messages,
        "The model could not produce a valid refined floor plan.",
        operation="floorplan-refine",
    )
