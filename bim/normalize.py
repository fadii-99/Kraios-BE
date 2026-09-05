"""Fill in what the model left out, and say so.

This runs on the RAW dict the model returned, before pydantic validates it, and
that ordering is the whole point. Once `BimPlan` has been constructed, a field
the model omitted is indistinguishable from one it set to the same value as the
default — so the only moment at which "the drawing did not say" can be
recorded is before validation.

Everything filled in here lands in `assumptions`, which the viewer renders as a
guess the user can correct. A default applied silently is a lie the user finds
out about when their quantities are wrong.

Wall thicknesses and opening dimensions are recorded as ONE aggregate
assumption each rather than one per element: a forty-wall plan with no stated
thicknesses should tell the user "no wall thickness was shown on this drawing",
not produce forty identical rows.
"""
from __future__ import annotations

from typing import Any, Dict, List

from bim.schema import SCHEMA_VERSION, BuildingType, defaults_for

# Opening types that sit on the floor and take the door defaults.
_DOOR_TYPES = {"door", "double_door", "sliding_door"}


def apply_defaults(raw: Dict[str, Any], *, building_type: BuildingType) -> Dict[str, Any]:
    """Return a copy of `raw` with omitted fields filled from type defaults.

    Never overwrites a value the model supplied, including a deliberate zero.
    Only genuinely absent keys and explicit nulls are filled.
    """
    plan = dict(raw)
    defaults = defaults_for(building_type)
    assumptions: List[dict] = list(plan.get("assumptions") or [])

    plan.setdefault("schema_version", SCHEMA_VERSION)
    plan.setdefault("units", "meters")
    if not plan.get("building_type"):
        plan["building_type"] = building_type.value

    # -- levels -----------------------------------------------------------
    levels = plan.get("levels")
    if not levels:
        levels = [{"id": "L1", "name": "Ground Floor", "elevation": 0.0}]
        assumptions.append(
            _assumption(
                "levels",
                None,
                "The drawing did not identify a storey; a single ground floor was assumed.",
            )
        )
    normalised_levels = []
    for index, level in enumerate(levels):
        level = dict(level)
        level.setdefault("id", f"L{index + 1}")
        level.setdefault("name", f"Level {index + 1}")
        level.setdefault("elevation", float(index) * defaults.floor_to_floor)
        for field, value, why in (
            (
                "floor_to_floor",
                defaults.floor_to_floor,
                "Floor-to-floor height is not shown on a plan view.",
            ),
            (
                "wall_height",
                defaults.wall_height,
                "Wall height is not shown on a plan view.",
            ),
            (
                "slab_thickness",
                defaults.slab_thickness,
                "Slab thickness is not shown on a plan view.",
            ),
        ):
            if level.get(field) is None:
                level[field] = value
                assumptions.append(
                    _assumption(f"{level['id']}.{field}", value, f"{why} Used the "
                                f"{building_type.value} default.")
                )
        normalised_levels.append(level)
    plan["levels"] = normalised_levels

    default_level_id = normalised_levels[0]["id"]

    # -- walls ------------------------------------------------------------
    walls = [dict(wall) for wall in (plan.get("walls") or [])]
    thickness_filled = 0
    for index, wall in enumerate(walls):
        wall.setdefault("id", f"W{index + 1:03d}")
        wall.setdefault("level_id", default_level_id)
        wall.setdefault("type", "interior")
        if wall.get("thickness") is None:
            wall["thickness"] = _default_thickness(wall.get("type"), defaults)
            thickness_filled += 1
    plan["walls"] = walls
    if thickness_filled:
        assumptions.append(
            _assumption(
                "walls.thickness",
                None,
                f"{thickness_filled} wall(s) had no thickness on the drawing; the "
                f"{building_type.value} defaults were used "
                f"({defaults.exterior_thickness:.2f} m exterior, "
                f"{defaults.interior_thickness:.2f} m interior).",
            )
        )

    # -- openings ---------------------------------------------------------
    openings = [dict(opening) for opening in (plan.get("openings") or [])]
    dimension_filled = 0
    for index, opening in enumerate(openings):
        opening.setdefault("id", f"O{index + 1:03d}")
        kind = str(opening.get("type") or "door").lower()
        is_door = kind in _DOOR_TYPES
        if opening.get("height") is None:
            opening["height"] = defaults.door_height if is_door else defaults.window_height
            dimension_filled += 1
        if opening.get("width") is None:
            opening["width"] = defaults.door_width if is_door else defaults.door_width
            dimension_filled += 1
        if opening.get("sill") is None:
            opening["sill"] = 0.0 if is_door else defaults.window_sill
            dimension_filled += 1
    plan["openings"] = openings
    if dimension_filled:
        assumptions.append(
            _assumption(
                "openings.dimensions",
                None,
                f"{dimension_filled} opening dimension(s) were not shown on the drawing; "
                f"the {building_type.value} defaults were used.",
            )
        )

    # -- rooms and fixtures ----------------------------------------------
    rooms = []
    for index, room in enumerate(plan.get("rooms") or []):
        room = dict(room)
        room.setdefault("id", f"R{index + 1:03d}")
        room.setdefault("level_id", default_level_id)
        rooms.append(room)
    plan["rooms"] = rooms

    fixtures = []
    for index, fixture in enumerate(plan.get("fixtures") or []):
        fixture = dict(fixture)
        fixture.setdefault("id", f"F{index + 1:03d}")
        fixture.setdefault("level_id", default_level_id)
        fixtures.append(fixture)
    plan["fixtures"] = fixtures

    plan["assumptions"] = assumptions
    return plan


def _default_thickness(wall_type: Any, defaults) -> float:
    mapping = {
        "exterior": defaults.exterior_thickness,
        "retaining": defaults.exterior_thickness,
        "interior": defaults.interior_thickness,
        "partition": defaults.partition_thickness,
    }
    return mapping.get(str(wall_type or "interior").lower(), defaults.interior_thickness)


def _assumption(target: str, value: Any, reason: str) -> dict:
    return {
        "target": target,
        "value": value,
        "confidence": "assumed",
        "reason": reason[:300],
    }
