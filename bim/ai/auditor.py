"""The visual audit: does the extracted model actually resemble the drawing?

The deterministic grader can only see the JSON. It will happily award 100 to a
plan that is internally perfect and bears no relationship to the uploaded
image — a real failure mode, because a vision model that misreads a drawing
misreads it *consistently*. This pass is the only check that looks at both.

It is given a text summary rather than a rendered image of the model, because
rendering the model would mean building the 3D pipeline to run the QA gate for
the 3D pipeline. A summary — dimensions, room names and areas, opening counts —
is enough to catch the failures that matter: missing rooms, invented rooms, and
a scale that is out by an order of magnitude.

FAILS OPEN, ALWAYS. Every error path returns `None`, and the caller proceeds on
the geometry score alone. A QA gate that can take down the pipeline it guards
is worse than no gate.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from bim.ai import jsonio, prompts
from bim.ai.client import BimModelError, complete_json, vision_messages
from bim.ai.config import bim_ai_settings
from bim.grading import geom
from bim.schema import BimPlan, OpeningType

logger = logging.getLogger(__name__)

# Rooms listed in the summary. Beyond this the prompt gets long without getting
# more useful — the auditor is checking for gross mismatch, not auditing row 40.
_MAX_ROOMS_IN_SUMMARY = 40


def audit(plan: BimPlan, image_data_url: str) -> Tuple[Optional[int], List[str]]:
    """Return (score 0-100, notes). `(None, [])` when the audit did not run."""
    if not bim_ai_settings.AUDIT_ENABLED:
        return None, []
    if not bim_ai_settings.is_configured():
        return None, []

    try:
        reply = complete_json(
            vision_messages(
                prompts.AUDIT_SYSTEM,
                prompts.AUDIT_USER_TEMPLATE.format(summary=summarise(plan)),
                [image_data_url],
            ),
            model=bim_ai_settings.MODEL_AUDIT,
            max_tokens=bim_ai_settings.MAX_TOKENS_AUDIT,
            operation="audit",
            temperature=0.0,
        )
        payload = jsonio.parse_object(reply.text)
    except (BimModelError, ValueError) as exc:
        logger.warning("bim.audit skipped — %s: %s", type(exc).__name__, exc)
        return None, []

    score = _clamped_score(payload.get("score"))
    if score is None:
        logger.warning("bim.audit returned no usable score; treating as not run")
        return None, []

    return score, _notes(payload)


def summarise(plan: BimPlan) -> str:
    """A compact, checkable description of the model, for the auditor to read."""
    lines: List[str] = []

    lines.append(f"Building type: {plan.building_type.value}")
    lines.append(f"Scale established from: {plan.scale.source.value}")
    if plan.scale.evidence:
        lines.append(f"Scale evidence: {plan.scale.evidence}")

    for level in plan.levels:
        walls = [w for w in plan.walls if w.level_id == level.id]
        rooms = [r for r in plan.rooms if r.level_id == level.id]
        lines.append("")
        lines.append(f"LEVEL '{level.name}' (elevation {level.elevation:.2f} m)")

        if level.outline:
            min_x, min_y, max_x, max_y = geom.bbox(level.outline)
            area = geom.polygon_area(level.outline)
            lines.append(
                f"  Overall extent: {max_x - min_x:.2f} m wide x {max_y - min_y:.2f} m deep"
                f"  (footprint {area:.1f} m², outline has {len(level.outline)} corners)"
            )

        wall_ids = {w.id for w in walls}
        level_openings = [o for o in plan.openings if o.wall_id in wall_ids]
        doors = [
            o
            for o in level_openings
            if o.type in (OpeningType.DOOR, OpeningType.DOUBLE_DOOR, OpeningType.SLIDING_DOOR)
        ]
        windows = [o for o in level_openings if o.type is OpeningType.WINDOW]
        lines.append(
            f"  {len(walls)} walls, {len(doors)} doors, {len(windows)} windows, "
            f"{len(rooms)} rooms"
        )

        if rooms:
            lines.append("  Rooms:")
            for room in rooms[:_MAX_ROOMS_IN_SUMMARY]:
                area = geom.polygon_area(room.polygon)
                min_x, min_y, max_x, max_y = geom.bbox(room.polygon)
                lines.append(
                    f"    - {room.name}: {area:.1f} m² "
                    f"({max_x - min_x:.1f} m x {max_y - min_y:.1f} m)"
                )
            if len(rooms) > _MAX_ROOMS_IN_SUMMARY:
                lines.append(f"    - ...and {len(rooms) - _MAX_ROOMS_IN_SUMMARY} more")

    if plan.description:
        lines.append("")
        lines.append(f"Extractor's own description: {plan.description}")

    return "\n".join(lines)


def _clamped_score(value) -> Optional[int]:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, score))


def _notes(payload: dict) -> List[str]:
    """Flatten the auditor's structured findings into display lines."""
    notes: List[str] = []

    verdict = payload.get("verdict")
    if isinstance(verdict, str) and verdict.strip():
        notes.append(verdict.strip())

    for item in payload.get("mismatches") or []:
        if isinstance(item, str) and item.strip():
            notes.append(item.strip())

    missed = [r for r in (payload.get("missed_rooms") or []) if isinstance(r, str)]
    if missed:
        notes.append("Rooms on the drawing but missing from the model: " + ", ".join(missed))

    phantom = [r for r in (payload.get("phantom_rooms") or []) if isinstance(r, str)]
    if phantom:
        notes.append("Rooms in the model that are not on the drawing: " + ", ".join(phantom))

    if payload.get("scale_looks_wrong"):
        comment = payload.get("scale_comment")
        notes.append(
            "The overall scale looks wrong"
            + (f": {comment}" if isinstance(comment, str) and comment.strip() else ".")
        )

    # Truncated because these go straight into a UI panel and a model in a bad
    # mood can produce thirty bullet points about one drawing.
    return [note[:400] for note in notes[:15]]
