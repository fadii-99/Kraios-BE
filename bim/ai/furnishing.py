"""The furniture pass: read every desk, chair, wc and basin off the drawing.

WHY THIS IS NOT PART OF THE GEOMETRY PASS
-----------------------------------------
The two compete for the same attention. Asked for walls and furniture in one
call, a model spends its effort on the hundred repeating workstations in an
open-plan office and its wall coordinates get measurably worse — and a plan with
imprecise walls is not a building, while a plan with no chairs is. Splitting them
also means the furniture list has its own token budget (a desk AND a chair per
workstation adds up) and its own failure: if this pass returns nothing, the
building is still there.

FAILS OPEN, ALWAYS. Every error path returns the plan unchanged.

WHAT IT DOES NOT DO
-------------------
It does not invent furniture. A room drawn empty stays empty — the prompt says
so, and `_plausible` drops anything that lands outside the building rather than
guessing where it was meant to go.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from bim.ai import jsonio, prompts
from bim.ai.client import BimModelError, complete_json, vision_messages
from bim.ai.config import bim_ai_settings
from bim.grading import geom
from bim.schema import BimPlan

logger = logging.getLogger(__name__)

# A fixture smaller than this in either direction is a line the model read as an
# object. A chair is 0.45 m; nothing real is below this.
MIN_FIXTURE_SIDE = 0.15

# Bigger than this and it is a room, not a piece of furniture. A boardroom table
# can be 6 m, so the ceiling is generous.
MAX_FIXTURE_SIDE = 12.0

# How far outside the building outline a fixture may sit before it is dropped.
# Not zero: a desk pushed against a wall legitimately has its centre a few
# centimetres inside, and the outline itself is an approximation.
OUTSIDE_TOLERANCE = 1.0

# Fallback heights by category, applied when the model omits one. Keys are
# matched as substrings, longest first, so "reception_desk" beats "desk".
DEFAULT_HEIGHTS = {
    "wardrobe": 2.0,
    "partition": 1.4,
    "bookshelf": 1.8,
    "shelf": 1.8,
    "urinal": 1.1,
    "cabinet": 0.9,
    "counter": 0.9,
    "basin": 0.85,
    "chair": 0.9,
    "plant": 1.2,
    "stove": 0.9,
    "fridge": 1.7,
    "table": 0.75,
    "desk": 0.75,
    "sofa": 0.8,
    "sink": 0.9,
    "bath": 0.6,
    "bed": 0.5,
    "wc": 0.8,
}


def furnish(plan: BimPlan, image_data_url: str) -> BimPlan:
    """Return `plan` with its fixtures read from the drawing. Never raises."""
    if not bim_ai_settings.FURNITURE_ENABLED or not bim_ai_settings.is_configured():
        return plan
    if not plan.rooms and not plan.levels:
        return plan

    try:
        reply = complete_json(
            vision_messages(
                prompts.FURNITURE_SYSTEM,
                prompts.FURNITURE_USER_TEMPLATE.format(rooms_block=_room_schedule(plan)),
                [image_data_url],
            ),
            model=bim_ai_settings.MODEL_FURNITURE,
            max_tokens=bim_ai_settings.MAX_TOKENS_FURNITURE,
            operation="furniture",
            temperature=0.1,
        )
        payload = jsonio.parse_object(reply.text)
    except (BimModelError, ValueError) as exc:
        logger.warning("bim.furniture skipped — %s: %s", type(exc).__name__, exc)
        return plan

    fixtures = _normalise(payload.get("fixtures"), plan)
    if not fixtures:
        logger.info("bim.furniture returned nothing usable; keeping the plan as it was")
        return plan

    try:
        furnished = plan.model_copy(deep=True)
        furnished.fixtures = []
        # Re-validated as a whole rather than trusting the list: a fixture on a
        # level that does not exist would otherwise reach the viewer and the IFC
        # builder as a dangling reference.
        result = BimPlan.model_validate(
            {**furnished.model_dump(mode="json"), "fixtures": fixtures}
        )
    except ValidationError as exc:
        logger.warning("bim.furniture produced an invalid plan, discarding it — %s", exc)
        return plan

    logger.info(
        "bim.furniture placed %d fixture(s) from %d candidate(s)",
        len(result.fixtures),
        len(payload.get("fixtures") or []),
    )
    return result


def _room_schedule(plan: BimPlan) -> str:
    """The rooms, with their extents, so the model can place things inside them.

    Bounding boxes rather than full polygons: this is a placement aid, and a
    hundred polygon vertices would crowd out the instructions that matter.
    """
    lines: List[str] = []
    for level in plan.levels:
        if level.outline:
            min_x, min_y, max_x, max_y = geom.bbox(level.outline)
            lines.append(
                f"LEVEL {level.id} '{level.name}' — building extent "
                f"x {min_x:.2f}..{max_x:.2f}, y {min_y:.2f}..{max_y:.2f}"
            )
        for room in plan.rooms:
            if room.level_id != level.id:
                continue
            min_x, min_y, max_x, max_y = geom.bbox(room.polygon)
            area = geom.polygon_area(room.polygon)
            lines.append(
                f"  {room.id}  {room.name}  —  x {min_x:.2f}..{max_x:.2f}, "
                f"y {min_y:.2f}..{max_y:.2f}  ({area:.1f} m²)"
            )
    if not lines:
        return "(No rooms were traced; place fixtures by the coordinates you read.)"
    return "\n".join(lines)


def _normalise(raw: Any, plan: BimPlan) -> List[dict]:
    """Clean the model's fixture list into rows `BimPlan` will accept."""
    if not isinstance(raw, list):
        return []

    rooms = {room.id: room for room in plan.rooms}
    level_ids = {level.id for level in plan.levels}
    default_level = plan.levels[0].id if plan.levels else "L1"

    outlines = [level.outline for level in plan.levels if len(level.outline) >= 3]

    fixtures: List[dict] = []
    for index, item in enumerate(raw):
        if len(fixtures) >= bim_ai_settings.MAX_FIXTURES:
            logger.warning(
                "bim.furniture stopped at the %d-fixture ceiling",
                bim_ai_settings.MAX_FIXTURES,
            )
            break

        fixture = _one(item, index, rooms, level_ids, default_level)
        if fixture is None:
            continue
        if not _plausible(fixture, outlines):
            continue
        fixtures.append(fixture)

    return fixtures


def _one(
    item: Any,
    index: int,
    rooms: Dict[str, Any],
    level_ids: set,
    default_level: str,
) -> Optional[dict]:
    if not isinstance(item, dict):
        return None

    category = str(item.get("category") or "").strip().lower()
    if not category:
        return None

    position = _point(item.get("position"))
    size = _point(item.get("size"))
    if position is None or size is None:
        return None

    width, depth = abs(size[0]), abs(size[1])
    if not (MIN_FIXTURE_SIDE <= width <= MAX_FIXTURE_SIDE):
        return None
    if not (MIN_FIXTURE_SIDE <= depth <= MAX_FIXTURE_SIDE):
        return None

    room_id = str(item.get("room_id") or "").strip().upper()
    if room_id not in rooms:
        room_id = ""

    # The level comes from the room when there is one, because a fixture and the
    # room it stands in cannot be on different storeys.
    level_id = rooms[room_id].level_id if room_id else str(item.get("level_id") or "")
    if level_id not in level_ids:
        level_id = default_level

    return {
        "id": f"F{index + 1:03d}",
        "category": category[:60],
        "position": position,
        "size": (round(width, 3), round(depth, 3)),
        "height": _height(item.get("height"), category),
        "rotation": _rotation(item.get("rotation")),
        "room_id": room_id,
        "level_id": level_id,
    }


def _plausible(fixture: dict, outlines: List[list]) -> bool:
    """Drop a fixture that sits outside the building.

    True when there is no outline to test against — refusing everything because
    the extractor produced no footprint would throw away good furniture over a
    missing field.
    """
    if not outlines:
        return True

    x, y = fixture["position"]
    for outline in outlines:
        if geom.point_in_polygon((x, y), outline):
            return True
        # Near enough counts: a fixture against an exterior wall can read as
        # marginally outside an outline traced along the wall centreline.
        edges = zip(outline, outline[1:] + outline[:1])
        if any(
            geom.point_to_segment_distance((x, y), a, b) <= OUTSIDE_TOLERANCE
            for a, b in edges
        ):
            return True
    return False


def _point(value: Any) -> Optional[tuple]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        return (round(float(value[0]), 3), round(float(value[1]), 3))
    except (TypeError, ValueError):
        return None


def _height(value: Any, category: str) -> float:
    try:
        height = float(value)
        if 0.05 <= height <= 10.0:
            return round(height, 3)
    except (TypeError, ValueError):
        pass
    # Longest key first, so "reception_desk" matches "desk" only after the more
    # specific entries have had their chance.
    for key in sorted(DEFAULT_HEIGHTS, key=len, reverse=True):
        if key in category:
            return DEFAULT_HEIGHTS[key]
    return 0.8


def _rotation(value: Any) -> float:
    try:
        return round(float(value) % 360.0, 2)
    except (TypeError, ValueError):
        return 0.0
