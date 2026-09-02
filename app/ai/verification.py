"""Fidelity guardrails for the 2D->3D pipeline.

Two ADDITIVE, FAIL-OPEN checks (a verifier error never breaks generation):

* Gate 1 - audit the extracted FloorPlan JSON against the original 2D plan
  BEFORE the browser builds the Three.js snapshot (catches the "wrong snapshot"
  class of failures).
* Lean render check - ONE fast-judge verify of the final Gemini render, with
  at most ONE corrective retry. (The former best-of-N Gate 2 — 2 parallel
  generations + up to 3 slow-judge verifies on EVERY request — was removed for
  latency; this lean check only costs a second generation when the first one
  actually failed.)

The prompts here are QA/verification prompts. They are intentionally separate
from the render/design prompts in ``prompts.py`` / ``rendering.py`` and do not
modify them.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

from app.ai import openrouter_floorplan_client as openrouter
from app.ai.config import ai_settings
from app.ai.floorplan_schema import FloorPlan
from app.ai.floorplan_snapshot_renderer import render_floorplan_snapshot_bytes

# --------------------------------------------------------------------------- #
# QA prompts (NOT design prompts)                                             #
# --------------------------------------------------------------------------- #

_JUDGE_SYSTEM = (
    "You are a meticulous architectural QA inspector. You compare two images and "
    "report ONLY measurable facts about layout fidelity. You never invent detail "
    "and you never soften a mismatch. Respond with a single strict JSON object "
    "and nothing else."
)

_JUDGE_JSON_CONTRACT = (
    "Return EXACTLY this JSON shape:\n"
    "{\n"
    '  "plan": {"rooms": <int>, "doors": <int>, "windows": <int>},\n'
    '  "candidate": {"rooms": <int>, "doors": <int>, "windows": <int>},\n'
    '  "layout_matches": <true|false>,\n'
    '  "fidelity_score": <int 0-100>,\n'
    '  "discrepancies": ["short factual phrase", ...]\n'
    "}\n"
    "Rules:\n"
    "- `plan` counts come from IMAGE 1 (the ground-truth 2D floor plan).\n"
    "- `candidate` counts come from IMAGE 2.\n"
    "- Set `layout_matches` to false if room/door/window counts differ OR the "
    "relative arrangement/position of rooms differs.\n"
    "- `fidelity_score`: 100 = identical layout; subtract heavily for missing or "
    "extra rooms/walls/openings and for moved walls. Furniture, colour, lighting "
    "and style do NOT affect the score.\n"
    "- Keep each discrepancy to a short phrase (e.g. 'render has 4 rooms, plan has 5')."
)

# Gate-1 contract: the massing snapshot NEVER draws doors or windows, so they
# must not count against it — otherwise the gate fails on every request and
# burns repair rounds that can never succeed.
_AUDIT_JSON_CONTRACT = (
    "Return EXACTLY this JSON shape:\n"
    "{\n"
    '  "plan": {"rooms": <int>, "doors": <int>, "windows": <int>},\n'
    '  "candidate": {"rooms": <int>, "doors": <int>, "windows": <int>},\n'
    '  "layout_matches": <true|false>,\n'
    '  "fidelity_score": <int 0-100>,\n'
    '  "discrepancies": ["short factual phrase", ...]\n'
    "}\n"
    "Rules:\n"
    "- `plan` counts come from IMAGE 1 (the ground-truth 2D floor plan).\n"
    "- `candidate` counts come from IMAGE 2. IMAGE 2 is a bare massing shell "
    "that NEVER shows doors or windows - report 0 for both and do NOT treat "
    "that as a mismatch or list it as a discrepancy.\n"
    "- Set `layout_matches` to false ONLY if the room count differs OR the "
    "relative arrangement/position/proportions of rooms and walls differ.\n"
    "- `fidelity_score`: 100 = identical wall/room layout; subtract heavily for "
    "missing or extra rooms/walls and for moved walls. Doors, windows, "
    "furniture, colour, lighting and style do NOT affect the score.\n"
    "- Keep each discrepancy to a short phrase (e.g. 'candidate has 4 rooms, "
    "plan has 5')."
)

_AUDIT_USER = (
    "IMAGE 1 is the original 2D floor plan (ground truth).\n"
    "IMAGE 2 is a rough grey 3D massing model built from extracted geometry "
    "data. It intentionally shows ONLY walls, floor slab and room outlines - "
    "no doors, no windows, no furniture.\n"
    "Judge ONLY whether its walls and rooms match IMAGE 1 in count, arrangement "
    "and proportions.\n\n"
    + _AUDIT_JSON_CONTRACT
)

_VERIFY_USER = (
    "IMAGE 1 is the original 2D floor plan (ground truth).\n"
    "IMAGE 2 is a furnished 3D render that is supposed to depict the SAME "
    "building.\n"
    "Ignore furniture, materials, lighting and artistic style - judge ONLY "
    "whether the walls, rooms, doors and windows match IMAGE 1 in count and "
    "arrangement.\n\n"
    + _JUDGE_JSON_CONTRACT
)

# --------------------------------------------------------------------------- #
# Report type                                                                 #
# --------------------------------------------------------------------------- #

@dataclass
class FidelityReport:
    score: int = 0
    passed: bool = False
    layout_matches: bool = False
    plan_counts: dict = field(default_factory=dict)
    candidate_counts: dict = field(default_factory=dict)
    issues: List[str] = field(default_factory=list)
    error: Optional[str] = None  # set when the judge itself failed (fail-open)

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "passed": self.passed,
            "layout_matches": self.layout_matches,
            "plan_counts": self.plan_counts,
            "candidate_counts": self.candidate_counts,
            "issues": self.issues,
            "error": self.error,
        }


def _data_url(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def _loads_lenient(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip().rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        cleaned = re.sub(r",\s*([}\]])", r"\1", text)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end > start:
                return json.loads(cleaned[start : end + 1])
            raise


def _report_from_raw(raw: dict, min_score: int, extra_issues: List[str]) -> FidelityReport:
    plan_counts = raw.get("plan") or {}
    cand_counts = raw.get("candidate") or {}
    layout_matches = bool(raw.get("layout_matches", False))
    try:
        score = int(raw.get("fidelity_score", 0))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))
    issues = list(raw.get("discrepancies") or [])
    issues.extend(extra_issues)
    # A structural sanity failure can only lower confidence, never raise it.
    if extra_issues:
        layout_matches = False
        score = min(score, min_score - 1 if min_score > 0 else 0)
    passed = layout_matches and score >= min_score
    return FidelityReport(
        score=score,
        passed=passed,
        layout_matches=layout_matches,
        plan_counts=plan_counts,
        candidate_counts=cand_counts,
        issues=issues,
    )


# --------------------------------------------------------------------------- #
# Deterministic structural sanity (free, no AI)                               #
# --------------------------------------------------------------------------- #

def _polygon_area(points) -> float:
    n = len(points)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def structural_sanity(plan: FloorPlan) -> List[str]:
    """Cheap deterministic checks on the geometry. Returns a list of problems."""
    issues: List[str] = []

    if not plan.rooms:
        issues.append("no rooms extracted")

    if plan.outline and _polygon_area(plan.outline) <= 0.5:
        issues.append("building outline has near-zero area")

    for i, room in enumerate(plan.rooms):
        if _polygon_area(room.polygon) <= 0.25:
            issues.append(f"room '{room.name or i}' has near-zero area")

    # Openings must physically fit on the wall they reference.
    for i, op in enumerate(plan.openings):
        if op.wall_index >= len(plan.walls):
            issues.append(f"opening {i} references a non-existent wall")
            continue
        wall = plan.walls[op.wall_index]
        wall_len = ((wall.end[0] - wall.start[0]) ** 2 + (wall.end[1] - wall.start[1]) ** 2) ** 0.5
        if op.offset > wall_len + 0.05:
            issues.append(f"opening {i} starts beyond the end of its wall")

    return issues


# --------------------------------------------------------------------------- #
# Async judge core                                                            #
# --------------------------------------------------------------------------- #

async def _judge(
    plan_png: bytes,
    candidate_png: bytes,
    user_prompt: str,
    operation: str,
    model: str | None = None,
) -> dict:
    messages = openrouter.build_messages_multi(
        _JUDGE_SYSTEM,
        user_prompt,
        [_data_url(plan_png), _data_url(candidate_png)],
    )
    raw = await openrouter.complete(messages, operation=operation, model=model)
    return _loads_lenient(raw)


async def audit_floorplan(plan_png: bytes, plan: FloorPlan) -> FidelityReport:
    """Gate 1: does the extracted geometry match the 2D plan? Fails open."""
    extra = structural_sanity(plan)
    try:
        snapshot_png = render_floorplan_snapshot_bytes(plan)
        raw = await _judge(plan_png, snapshot_png, _AUDIT_USER, "fidelity-audit (gate1)")
        return _report_from_raw(raw, ai_settings.FIDELITY_MIN_SCORE, extra)
    except Exception as exc:  # fail-open: never block generation on a QA error
        return FidelityReport(
            score=0, passed=True, layout_matches=True, issues=extra, error=str(exc)
        )


# --------------------------------------------------------------------------- #
# Sync wrappers (job pipeline runs in a worker thread)                        #
# --------------------------------------------------------------------------- #

def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def verify_render(plan_png: bytes, render_png: bytes) -> FidelityReport:
    """Lean render check: does the final render match the 2D plan? Uses the
    FAST judge model (RENDER_VERIFY_MODEL), not the extraction model. Fails
    open."""
    try:
        raw = await _judge(
            plan_png,
            render_png,
            _VERIFY_USER,
            "render-verify",
            model=ai_settings.RENDER_VERIFY_MODEL,
        )
        return _report_from_raw(raw, ai_settings.RENDER_VERIFY_MIN_SCORE, [])
    except Exception as exc:  # fail-open
        return FidelityReport(
            score=0, passed=True, layout_matches=True, error=str(exc)
        )


def verify_render_sync(plan_png: bytes, render_png: bytes) -> FidelityReport:
    return _run_async(verify_render(plan_png, render_png))
