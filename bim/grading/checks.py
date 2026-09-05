"""The deterministic grader: every rule, its tolerance, and its repair.

This runs before any AI is asked for an opinion, and it is the part of the
system that makes an arbitrary floor plan safe to build from. A vision model
asked to read a drawing will, reliably and at some rate, produce a wall a few
millimetres short of its neighbour, a door wider than the wall hosting it, or a
room polygon that crosses itself. None of those are visible in the JSON and all
of them produce a visibly broken 3D model. They are cheap to detect and mostly
cheap to fix, so they are detected and fixed here rather than being re-prompted
for.

REPAIR POLICY
-------------
A repair runs only when there is exactly one sensible correction. Snapping two
endpoints 8 mm apart is unambiguous. Deciding which of two overlapping rooms is
the real one is not, so that stays a warning and the user decides.

Every repair is recorded as an `Issue` with `repair` set, never applied
silently, and still costs score — see `report.py` for why.

TOLERANCES
----------
The constants below are the whole policy of this module and the first place to
look when a real plan grades badly. They are stated in meters and chosen for
drawings extracted from an image at a plausible scale, where sub-centimetre
precision is noise rather than signal.
"""
from __future__ import annotations

import math
from typing import Dict, List, Tuple

from bim.grading import geom
from bim.grading.report import Issue, QualityReport, Severity
from bim.schema import (
    Assumption,
    BimPlan,
    Confidence,
    Level,
    Opening,
    OpeningType,
    Room,
    ScaleSource,
    Wall,
    WallType,
    defaults_for,
)

# Two wall endpoints closer than this are meant to be the same corner. Set from
# what a vision model actually produces: it rounds coordinates to 2-3 decimals
# and accumulates error along a run of walls, so misses cluster under ~5 cm.
# Raising this starts merging genuinely distinct corners in tight plans
# (a 10 cm service duct between two rooms).
SNAP_TOLERANCE = 0.05

# A wall shorter than this is a coordinate typo, not a wall. Below it there is
# nothing to host an opening and nothing visible in the model.
MIN_WALL_LENGTH = 0.10

# Beyond this a "wall" is almost certainly a mis-scaled plan rather than a real
# element. Warned about, never removed — long walls do exist in warehouses.
MAX_PLAUSIBLE_WALL_LENGTH = 200.0

# Wall thickness outside this band is implausible for any building type and is
# clamped to the building type's default.
MIN_WALL_THICKNESS = 0.05
MAX_WALL_THICKNESS = 1.50

# A room smaller than this is a fragment of a mis-traced polygon.
MIN_ROOM_AREA = 0.50

# Two rooms sharing more than this fraction of the smaller one's area are
# double-traced. 5% absorbs the sampling estimate's noise and the odd shared
# boundary strip without missing a genuine duplicate.
MAX_ROOM_OVERLAP_RATIO = 0.05

# An opening must leave at least this much wall on each side, or it is really a
# full-width opening and the wall segment should not exist.
MIN_OPENING_MARGIN = 0.02

# Plausible door and window dimensions. Outside these the extraction is
# suspect, but shopfronts and warehouse shutters are legitimately huge, so
# these are warnings that never repair.
PLAUSIBLE_DOOR_WIDTH = (0.55, 6.0)
PLAUSIBLE_WINDOW_WIDTH = (0.30, 12.0)
MAX_PLAUSIBLE_WINDOW_SILL = 2.6

# Overall footprint sanity. Below the floor a "building" is a mis-scaled plan;
# above the ceiling it is a site plan read as a building.
MIN_PLAUSIBLE_FOOTPRINT = 4.0
MAX_PLAUSIBLE_FOOTPRINT = 200_000.0

# When traced rooms cover less than this fraction of the building outline, the
# extractor probably stopped early and missed rooms. A real plan has corridors
# and wall thickness that rooms do not cover, so this is set low on purpose.
MIN_ROOM_COVERAGE = 0.35

# An endpoint further than this from every other wall is a dangling stub. Set
# looser than SNAP_TOLERANCE because a legitimately free wall end (a garden
# wall, a partition stub) should not be reported for being 10 cm from nothing.
DANGLING_TOLERANCE = 0.25


def grade(plan: BimPlan) -> Tuple[BimPlan, QualityReport]:
    """Check and repair a plan. Returns the repaired copy and its report.

    The input is never mutated: callers keep the raw extraction so a bad repair
    can be diagnosed against what the model actually returned.

    Order is deliberate. Snapping runs first because a snapped corner removes
    dangling-end and unclosed-outline findings that would otherwise be reported
    against a plan that was only ever a few millimetres out. Wall removal runs
    before opening checks because an opening's host may be about to disappear.
    """
    plan = plan.model_copy(deep=True)
    report = QualityReport()

    _snap_wall_endpoints(plan, report)
    _drop_degenerate_walls(plan, report)
    _drop_duplicate_walls(plan, report)
    _clamp_wall_thickness(plan, report)
    _check_wall_plausibility(plan, report)
    _check_dangling_ends(plan, report)

    _repair_openings(plan, report)
    _check_opening_plausibility(plan, report)

    _repair_rooms(plan, report)
    _check_room_overlaps(plan, report)

    _repair_levels(plan, report)
    _check_plan_plausibility(plan, report)

    report.stats = _stats(plan)
    return plan, report


# --------------------------------------------------------------------------
# Walls
# --------------------------------------------------------------------------
def _snap_wall_endpoints(plan: BimPlan, report: QualityReport) -> None:
    """Merge endpoints that are within SNAP_TOLERANCE of each other.

    This is the highest-value repair in the module. A vision model emits each
    wall independently, so a corner shared by two walls arrives as two points a
    few millimetres apart. In JSON that is invisible; in 3D it is a gap of light
    at every corner of the building, and it defeats any later attempt to derive
    rooms from wall loops.

    Endpoints are clustered against representatives rather than pairwise-merged
    so a run of three near-coincident points collapses to one, not to a chain
    that drifts. Per level, because two storeys legitimately have corners at the
    same (x, y).
    """
    by_level: Dict[str, List[Tuple[Wall, str]]] = {}
    for wall in plan.walls:
        by_level.setdefault(wall.level_id, []).append((wall, "start"))
        by_level.setdefault(wall.level_id, []).append((wall, "end"))

    snapped = 0
    for _level_id, entries in by_level.items():
        representatives: List[geom.Point] = []
        for wall, which in entries:
            point = wall.start if which == "start" else wall.end
            match = next(
                (r for r in representatives if geom.distance(point, r) <= SNAP_TOLERANCE),
                None,
            )
            if match is None:
                representatives.append(point)
                continue
            if match == point:
                continue
            if which == "start":
                wall.start = match
            else:
                wall.end = match
            snapped += 1

    if snapped:
        report.add(
            Issue(
                code="WALL_ENDPOINT_SNAPPED",
                severity=Severity.WARNING,
                message=(
                    f"{snapped} wall endpoint(s) were within {SNAP_TOLERANCE * 100:.0f} cm "
                    "of a neighbouring corner but not joined to it."
                ),
                repair=f"Snapped {snapped} endpoint(s) onto the nearest shared corner.",
                detail={"count": snapped, "tolerance_m": SNAP_TOLERANCE},
            )
        )


def _drop_degenerate_walls(plan: BimPlan, report: QualityReport) -> None:
    """Remove walls too short to be real, and any openings they hosted.

    Snapping can create these: two endpoints merge and a short wall between
    them collapses to nothing. That is the intended outcome — the wall was a
    tracing artefact — but the collapsed wall must not survive into the model.
    """
    keep: List[Wall] = []
    removed_ids: List[str] = []
    for wall in plan.walls:
        if wall.length < MIN_WALL_LENGTH:
            removed_ids.append(wall.id)
            continue
        keep.append(wall)

    if not removed_ids:
        return

    plan.walls = keep
    orphaned = [o.id for o in plan.openings if o.wall_id in removed_ids]
    plan.openings = [o for o in plan.openings if o.wall_id not in removed_ids]

    for wall_id in removed_ids:
        report.add(
            Issue(
                code="WALL_DEGENERATE",
                severity=Severity.ERROR,
                message=f"Wall {wall_id} is shorter than {MIN_WALL_LENGTH * 100:.0f} cm.",
                element_id=wall_id,
                element_kind="wall",
                repair="Removed the wall.",
            )
        )
    if orphaned:
        report.add(
            Issue(
                code="OPENING_ORPHANED",
                severity=Severity.ERROR,
                message=(
                    f"{len(orphaned)} opening(s) were hosted by a wall that had to be removed."
                ),
                repair=f"Removed opening(s): {', '.join(orphaned)}.",
                detail={"opening_ids": orphaned},
            )
        )


def _drop_duplicate_walls(plan: BimPlan, report: QualityReport) -> None:
    """Remove a wall drawn twice, keeping the first and rehosting its openings.

    Only exact-ish duplicates (both endpoints coincident, either direction) are
    removed. Partial collinear overlap is reported but left alone: deciding
    where one wall should end so the other can begin is a modelling decision,
    not a repair.
    """
    keep: List[Wall] = []
    # removed wall id -> (surviving wall id, the twin ran the other way)
    rehost: Dict[str, Tuple[str, bool]] = {}

    for wall in plan.walls:
        twin = None
        twin_is_reversed = False
        for existing in keep:
            if existing.level_id != wall.level_id:
                continue
            same_way = (
                geom.distance(existing.start, wall.start) <= SNAP_TOLERANCE
                and geom.distance(existing.end, wall.end) <= SNAP_TOLERANCE
            )
            reversed_way = (
                geom.distance(existing.start, wall.end) <= SNAP_TOLERANCE
                and geom.distance(existing.end, wall.start) <= SNAP_TOLERANCE
            )
            if same_way or reversed_way:
                twin = existing
                twin_is_reversed = reversed_way and not same_way
                break
        if twin is None:
            keep.append(wall)
        else:
            rehost[wall.id] = (twin.id, twin_is_reversed)

    if rehost:
        plan.walls = keep
        surviving = {w.id: w for w in keep}
        for opening in plan.openings:
            entry = rehost.get(opening.wall_id)
            if entry is None:
                continue
            survivor_id, was_reversed = entry
            survivor = surviving[survivor_id]
            opening.wall_id = survivor_id

            # An offset is measured from its wall's `start`. When the removed
            # twin ran the opposite way, that start was the survivor's END, so
            # the opening has to be mirrored along the wall — unconditionally,
            # from the direction recorded at merge time.
            #
            # Inferring the mirror instead ("did it end up past the wall's
            # end?") only catches an opening near the far end; one in the middle
            # of a reversed twin fits either way and would be left silently at
            # the wrong position, which is worse than not merging at all.
            if was_reversed:
                opening.offset = max(
                    0.0, survivor.length - opening.offset - opening.width
                )

        for removed_id, (survivor_id, _reversed) in rehost.items():
            report.add(
                Issue(
                    code="WALL_DUPLICATE",
                    severity=Severity.WARNING,
                    message=f"Wall {removed_id} duplicates wall {survivor_id}.",
                    element_id=removed_id,
                    element_kind="wall",
                    repair=f"Removed it and moved its openings onto {survivor_id}.",
                )
            )

    # Partial overlaps: report only.
    for index, wall in enumerate(plan.walls):
        for other in plan.walls[index + 1 :]:
            if other.level_id != wall.level_id:
                continue
            shared = geom.collinear_overlap(
                wall.start, wall.end, other.start, other.end, tolerance=SNAP_TOLERANCE
            )
            if shared > max(MIN_WALL_LENGTH, 0.2 * min(wall.length, other.length)):
                report.add(
                    Issue(
                        code="WALL_OVERLAP",
                        severity=Severity.WARNING,
                        message=(
                            f"Walls {wall.id} and {other.id} overlap along "
                            f"{shared:.2f} m of their length."
                        ),
                        element_id=wall.id,
                        element_kind="wall",
                        detail={"other_id": other.id, "overlap_m": round(shared, 3)},
                    )
                )


def _clamp_wall_thickness(plan: BimPlan, report: QualityReport) -> None:
    defaults = defaults_for(plan.building_type)
    replacement = {
        WallType.EXTERIOR: defaults.exterior_thickness,
        WallType.INTERIOR: defaults.interior_thickness,
        WallType.PARTITION: defaults.partition_thickness,
        WallType.RETAINING: defaults.exterior_thickness,
    }
    for wall in plan.walls:
        if MIN_WALL_THICKNESS <= wall.thickness <= MAX_WALL_THICKNESS:
            continue
        was = wall.thickness
        wall.thickness = replacement.get(wall.type, defaults.interior_thickness)
        report.add(
            Issue(
                code="WALL_THICKNESS_IMPLAUSIBLE",
                severity=Severity.WARNING,
                message=f"Wall {wall.id} had a thickness of {was:.3f} m.",
                element_id=wall.id,
                element_kind="wall",
                repair=f"Set it to the {plan.building_type.value} default, {wall.thickness:.2f} m.",
                detail={"was": was, "now": wall.thickness},
            )
        )
        plan.assumptions.append(
            Assumption(
                target=f"{wall.id}.thickness",
                value=wall.thickness,
                confidence=Confidence.ASSUMED,
                reason="The extracted thickness was outside the plausible range.",
            )
        )


def _check_wall_plausibility(plan: BimPlan, report: QualityReport) -> None:
    for wall in plan.walls:
        if wall.length > MAX_PLAUSIBLE_WALL_LENGTH:
            report.add(
                Issue(
                    code="WALL_TOO_LONG",
                    severity=Severity.WARNING,
                    message=(
                        f"Wall {wall.id} is {wall.length:.1f} m long, which suggests the "
                        "plan's scale was read incorrectly."
                    ),
                    element_id=wall.id,
                    element_kind="wall",
                    detail={"length_m": round(wall.length, 2)},
                )
            )


def _check_dangling_ends(plan: BimPlan, report: QualityReport) -> None:
    """Report wall ends that touch nothing.

    A few are normal — a partition stub, a boundary wall. Many mean the
    extractor traced walls as disconnected sticks, which produces a model full
    of gaps, so the count is what carries the signal, not any single end.
    """
    dangling = 0
    for wall in plan.walls:
        for point in (wall.start, wall.end):
            touches = False
            for other in plan.walls:
                if other.id == wall.id or other.level_id != wall.level_id:
                    continue
                near_endpoint = min(
                    geom.distance(point, other.start), geom.distance(point, other.end)
                )
                if near_endpoint <= DANGLING_TOLERANCE:
                    touches = True
                    break
                if geom.point_to_segment_distance(point, other.start, other.end) <= DANGLING_TOLERANCE:
                    touches = True
                    break
            if not touches:
                dangling += 1

    if not dangling:
        return

    total_ends = max(1, len(plan.walls) * 2)
    ratio = dangling / total_ends
    # One or two free ends in a real plan is unremarkable; a quarter of them is
    # a tracing failure.
    severity = Severity.WARNING if ratio > 0.15 else Severity.INFO
    report.add(
        Issue(
            code="WALL_ENDS_DANGLING",
            severity=severity,
            message=(
                f"{dangling} of {total_ends} wall ends do not meet another wall. "
                "Disconnected walls leave gaps in the 3D model."
            ),
            detail={"dangling": dangling, "total_ends": total_ends, "ratio": round(ratio, 3)},
        )
    )


# --------------------------------------------------------------------------
# Openings
# --------------------------------------------------------------------------
def _repair_openings(plan: BimPlan, report: QualityReport) -> None:
    """Make every opening physically fit inside its host wall.

    Three failures, in the order they have to be handled: an opening wider than
    its wall cannot be placed at all; one that merely runs off the end can be
    slid back; two that overlap each other have to be separated or dropped.
    """
    walls = {wall.id: wall for wall in plan.walls}
    keep: List[Opening] = []

    for opening in plan.openings:
        wall = walls.get(opening.wall_id)
        if wall is None:  # already reported as orphaned
            continue

        usable = wall.length - 2 * MIN_OPENING_MARGIN
        if usable <= 0 or opening.width > wall.length:
            report.add(
                Issue(
                    code="OPENING_WIDER_THAN_WALL",
                    severity=Severity.ERROR,
                    message=(
                        f"Opening {opening.id} is {opening.width:.2f} m wide but wall "
                        f"{wall.id} is only {wall.length:.2f} m long."
                    ),
                    element_id=opening.id,
                    element_kind="opening",
                    repair="Removed the opening.",
                )
            )
            continue

        if opening.width > usable:
            was = opening.width
            opening.width = usable
            report.add(
                Issue(
                    code="OPENING_FILLS_WALL",
                    severity=Severity.WARNING,
                    message=(
                        f"Opening {opening.id} left no wall either side of it."
                    ),
                    element_id=opening.id,
                    element_kind="opening",
                    repair=f"Narrowed it from {was:.2f} m to {opening.width:.2f} m.",
                    detail={"was": was, "now": opening.width},
                )
            )

        max_offset = wall.length - opening.width - MIN_OPENING_MARGIN
        if opening.offset < MIN_OPENING_MARGIN or opening.offset > max_offset:
            was = opening.offset
            opening.offset = min(max(MIN_OPENING_MARGIN, opening.offset), max(MIN_OPENING_MARGIN, max_offset))
            report.add(
                Issue(
                    code="OPENING_OUT_OF_BOUNDS",
                    severity=Severity.ERROR,
                    message=(
                        f"Opening {opening.id} sat at {was:.2f} m along a "
                        f"{wall.length:.2f} m wall and ran past its end."
                    ),
                    element_id=opening.id,
                    element_kind="opening",
                    repair=f"Moved it to {opening.offset:.2f} m along the wall.",
                    detail={"was": was, "now": opening.offset, "wall_length": round(wall.length, 3)},
                )
            )

        wall_height = plan.effective_wall_height(wall)
        if opening.sill + opening.height > wall_height:
            was = opening.height
            opening.height = max(0.3, wall_height - opening.sill - 0.05)
            report.add(
                Issue(
                    code="OPENING_TALLER_THAN_WALL",
                    severity=Severity.ERROR,
                    message=(
                        f"Opening {opening.id} reached {was + opening.sill:.2f} m in a "
                        f"{wall_height:.2f} m wall."
                    ),
                    element_id=opening.id,
                    element_kind="opening",
                    repair=f"Reduced its height from {was:.2f} m to {opening.height:.2f} m.",
                    detail={"was": was, "now": opening.height, "wall_height": wall_height},
                )
            )

        keep.append(opening)

    plan.openings = keep
    _separate_overlapping_openings(plan, report)


def _separate_overlapping_openings(plan: BimPlan, report: QualityReport) -> None:
    """Drop the smaller of two openings that occupy the same run of wall.

    Two openings overlapping is almost always the same door detected twice (a
    door and its swing arc read as two elements). Keeping the wider one is the
    right guess: the spurious detection is usually the smaller fragment.
    """
    by_wall: Dict[str, List[Opening]] = {}
    for opening in plan.openings:
        by_wall.setdefault(opening.wall_id, []).append(opening)

    dropped: set[str] = set()
    for _wall_id, group in by_wall.items():
        ordered = sorted(group, key=lambda o: o.offset)
        for index, opening in enumerate(ordered):
            if opening.id in dropped:
                continue
            for other in ordered[index + 1 :]:
                if other.id in dropped:
                    continue
                if other.offset >= opening.offset + opening.width:
                    break  # sorted, so nothing further can overlap
                loser, winner = (
                    (other, opening) if other.width <= opening.width else (opening, other)
                )
                dropped.add(loser.id)
                report.add(
                    Issue(
                        code="OPENING_OVERLAP",
                        severity=Severity.WARNING,
                        message=(
                            f"Openings {opening.id} and {other.id} overlap on wall "
                            f"{opening.wall_id}."
                        ),
                        element_id=loser.id,
                        element_kind="opening",
                        repair=f"Removed the narrower one; kept {winner.id}.",
                    )
                )
                if loser.id == opening.id:
                    break

    if dropped:
        plan.openings = [o for o in plan.openings if o.id not in dropped]


def _check_opening_plausibility(plan: BimPlan, report: QualityReport) -> None:
    doors = (OpeningType.DOOR, OpeningType.DOUBLE_DOOR, OpeningType.SLIDING_DOOR)
    for opening in plan.openings:
        if opening.type in doors:
            low, high = PLAUSIBLE_DOOR_WIDTH
            if not low <= opening.width <= high:
                report.add(
                    Issue(
                        code="DOOR_WIDTH_IMPLAUSIBLE",
                        severity=Severity.WARNING,
                        message=(
                            f"Door {opening.id} is {opening.width:.2f} m wide "
                            f"(expected {low:.2f}-{high:.2f} m)."
                        ),
                        element_id=opening.id,
                        element_kind="opening",
                        detail={"width": opening.width},
                    )
                )
        elif opening.type is OpeningType.WINDOW:
            low, high = PLAUSIBLE_WINDOW_WIDTH
            if not low <= opening.width <= high:
                report.add(
                    Issue(
                        code="WINDOW_WIDTH_IMPLAUSIBLE",
                        severity=Severity.WARNING,
                        message=(
                            f"Window {opening.id} is {opening.width:.2f} m wide "
                            f"(expected {low:.2f}-{high:.2f} m)."
                        ),
                        element_id=opening.id,
                        element_kind="opening",
                        detail={"width": opening.width},
                    )
                )
            if opening.sill > MAX_PLAUSIBLE_WINDOW_SILL:
                report.add(
                    Issue(
                        code="WINDOW_SILL_IMPLAUSIBLE",
                        severity=Severity.WARNING,
                        message=(
                            f"Window {opening.id} has a sill {opening.sill:.2f} m above "
                            "the floor."
                        ),
                        element_id=opening.id,
                        element_kind="opening",
                        detail={"sill": opening.sill},
                    )
                )


# --------------------------------------------------------------------------
# Rooms
# --------------------------------------------------------------------------
def _repair_rooms(plan: BimPlan, report: QualityReport) -> None:
    keep: List[Room] = []
    seen_names: Dict[Tuple[str, str], int] = {}

    for room in plan.rooms:
        # Self-intersection is tested BEFORE area, and the room is kept.
        # A bow-tie polygon's two lobes cancel out under the shoelace formula,
        # so it measures as zero area — testing area first would delete it as
        # "degenerate" and report a tracing error as a rounding error. It is
        # also not repaired: untangling a self-intersecting polygon has several
        # plausible answers, and picking the wrong one silently reshapes a room
        # the user can see. So it stays, visibly broken, as a hard error.
        if geom.polygon_self_intersects(room.polygon):
            report.add(
                Issue(
                    code="ROOM_SELF_INTERSECTING",
                    severity=Severity.ERROR,
                    message=(
                        f"Room '{room.name}' ({room.id}) has a boundary that crosses itself."
                    ),
                    element_id=room.id,
                    element_kind="room",
                )
            )
            keep.append(room)
            continue

        area = geom.polygon_area(room.polygon)
        if area < MIN_ROOM_AREA:
            report.add(
                Issue(
                    code="ROOM_DEGENERATE",
                    severity=Severity.WARNING,
                    message=(
                        f"Room '{room.name}' ({room.id}) encloses {area:.2f} m², below the "
                        f"{MIN_ROOM_AREA:.2f} m² minimum."
                    ),
                    element_id=room.id,
                    element_kind="room",
                    repair="Removed the room.",
                    detail={"area_m2": round(area, 3)},
                )
            )
            continue

        key = (room.level_id, room.name.strip().lower())
        count = seen_names.get(key, 0) + 1
        seen_names[key] = count
        if count > 1:
            was = room.name
            room.name = f"{room.name} {count}"
            report.add(
                Issue(
                    code="ROOM_DUPLICATE_NAME",
                    severity=Severity.INFO,
                    message=f"More than one room on this level is called '{was}'.",
                    element_id=room.id,
                    element_kind="room",
                    repair=f"Renamed to '{room.name}'.",
                )
            )

        keep.append(room)

    plan.rooms = keep


def _check_room_overlaps(plan: BimPlan, report: QualityReport) -> None:
    by_level: Dict[str, List[Room]] = {}
    for room in plan.rooms:
        by_level.setdefault(room.level_id, []).append(room)

    for _level_id, rooms in by_level.items():
        boxes = {room.id: geom.bbox(room.polygon) for room in rooms}
        for index, room in enumerate(rooms):
            for other in rooms[index + 1 :]:
                if not geom.bboxes_overlap(boxes[room.id], boxes[other.id]):
                    continue
                ratio = geom.polygons_overlap_ratio(room.polygon, other.polygon)
                if ratio <= MAX_ROOM_OVERLAP_RATIO:
                    continue
                report.add(
                    Issue(
                        code="ROOM_OVERLAP",
                        severity=Severity.WARNING,
                        message=(
                            f"Rooms '{room.name}' and '{other.name}' overlap by about "
                            f"{ratio * 100:.0f}% of the smaller one."
                        ),
                        element_id=room.id,
                        element_kind="room",
                        detail={"other_id": other.id, "overlap_ratio": round(ratio, 3)},
                    )
                )


# --------------------------------------------------------------------------
# Levels and whole-plan checks
# --------------------------------------------------------------------------
def _repair_levels(plan: BimPlan, report: QualityReport) -> None:
    for level in plan.levels:
        level_walls = [w for w in plan.walls if w.level_id == level.id]

        if not level.outline and level_walls:
            points = [p for wall in level_walls for p in (wall.start, wall.end)]
            level.outline = geom.convex_hull(points)
            report.add(
                Issue(
                    code="LEVEL_OUTLINE_MISSING",
                    severity=Severity.WARNING,
                    message=f"Level '{level.name}' had no building outline.",
                    element_id=level.id,
                    element_kind="level",
                    repair=(
                        "Derived one from the wall endpoints. This is a convex hull, so "
                        "an L-shaped or courtyard footprint will be wrong until corrected."
                    ),
                )
            )
            plan.assumptions.append(
                Assumption(
                    target=f"{level.id}.outline",
                    value=None,
                    confidence=Confidence.ASSUMED,
                    reason="No outline was extracted; a convex hull of the walls was used.",
                )
            )

        # Wall height has to leave room for the slab above it.
        headroom = level.floor_to_floor - level.slab_thickness
        if level.wall_height > headroom > 0:
            was = level.wall_height
            level.wall_height = round(headroom, 3)
            report.add(
                Issue(
                    code="LEVEL_HEIGHT_INCONSISTENT",
                    severity=Severity.WARNING,
                    message=(
                        f"Level '{level.name}' had a {was:.2f} m wall height inside a "
                        f"{level.floor_to_floor:.2f} m floor-to-floor with a "
                        f"{level.slab_thickness:.2f} m slab."
                    ),
                    element_id=level.id,
                    element_kind="level",
                    repair=f"Reduced the wall height to {level.wall_height:.2f} m.",
                )
            )

    # Levels must not sit at the same elevation or in the wrong order.
    ordered = sorted(plan.levels, key=lambda l: l.elevation)
    for lower, upper in zip(ordered, ordered[1:]):
        if math.isclose(lower.elevation, upper.elevation, abs_tol=1e-6):
            report.add(
                Issue(
                    code="LEVEL_ELEVATION_DUPLICATE",
                    severity=Severity.WARNING,
                    message=(
                        f"Levels '{lower.name}' and '{upper.name}' are both at "
                        f"{lower.elevation:.2f} m."
                    ),
                    element_id=upper.id,
                    element_kind="level",
                )
            )


def _check_plan_plausibility(plan: BimPlan, report: QualityReport) -> None:
    if plan.scale.source is ScaleSource.UNKNOWN:
        report.add(
            Issue(
                code="SCALE_UNKNOWN",
                severity=Severity.WARNING,
                message=(
                    "No dimension, scale bar or scale ratio was found on the drawing, so "
                    "every measurement is an estimate."
                ),
                detail={"confidence": plan.scale.confidence.value},
            )
        )
    elif plan.scale.source is ScaleSource.DOOR_HEURISTIC:
        report.add(
            Issue(
                code="SCALE_FROM_HEURISTIC",
                severity=Severity.INFO,
                message=(
                    "Scale was estimated from a standard door width rather than read from "
                    "the drawing. Overall dimensions may be off by 10-20%."
                ),
            )
        )

    footprint = sum(geom.polygon_area(level.outline) for level in plan.levels if level.outline)
    if footprint and not (MIN_PLAUSIBLE_FOOTPRINT <= footprint <= MAX_PLAUSIBLE_FOOTPRINT):
        report.add(
            Issue(
                code="FOOTPRINT_IMPLAUSIBLE",
                severity=Severity.ERROR,
                message=(
                    f"The building's total footprint works out to {footprint:.1f} m², which "
                    "is outside any plausible range. The plan's scale is probably wrong."
                ),
                detail={"footprint_m2": round(footprint, 2)},
            )
        )

    if not plan.rooms:
        report.add(
            Issue(
                code="NO_ROOMS",
                severity=Severity.WARNING,
                message=(
                    "No rooms were identified. The model will have walls but no named "
                    "spaces, areas or schedules."
                ),
            )
        )
    else:
        room_area = sum(geom.polygon_area(room.polygon) for room in plan.rooms)
        if footprint > 0:
            coverage = room_area / footprint
            if coverage < MIN_ROOM_COVERAGE:
                report.add(
                    Issue(
                        code="ROOM_COVERAGE_LOW",
                        severity=Severity.WARNING,
                        message=(
                            f"Traced rooms cover only {coverage * 100:.0f}% of the building "
                            "footprint, so rooms were probably missed."
                        ),
                        detail={
                            "coverage": round(coverage, 3),
                            "room_area_m2": round(room_area, 2),
                            "footprint_m2": round(footprint, 2),
                        },
                    )
                )

    doors = [
        o
        for o in plan.openings
        if o.type in (OpeningType.DOOR, OpeningType.DOUBLE_DOOR, OpeningType.SLIDING_DOOR)
    ]
    if not doors:
        report.add(
            Issue(
                code="NO_DOORS",
                severity=Severity.WARNING,
                message="No doors were found. Every plan has at least one way in.",
            )
        )


def _stats(plan: BimPlan) -> dict:
    footprint = sum(geom.polygon_area(level.outline) for level in plan.levels if level.outline)
    room_area = sum(geom.polygon_area(room.polygon) for room in plan.rooms)
    wall_length = sum(wall.length for wall in plan.walls)
    return {
        "levels": len(plan.levels),
        "walls": len(plan.walls),
        "openings": len(plan.openings),
        "doors": len(
            [
                o
                for o in plan.openings
                if o.type
                in (OpeningType.DOOR, OpeningType.DOUBLE_DOOR, OpeningType.SLIDING_DOOR)
            ]
        ),
        "windows": len([o for o in plan.openings if o.type is OpeningType.WINDOW]),
        "rooms": len(plan.rooms),
        "fixtures": len(plan.fixtures),
        "assumptions": len(plan.assumptions),
        "footprint_m2": round(footprint, 2),
        "room_area_m2": round(room_area, 2),
        "total_wall_length_m": round(wall_length, 2),
    }
