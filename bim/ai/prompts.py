"""Prompts for the two extraction passes, the repair round, and the audit.

The worked example at the bottom is not decoration: `tests/test_prompts.py`
validates it against `BimPlan`, so the contract shown to the model cannot drift
away from the contract the parser enforces. If you change the schema and forget
the prompt, that test fails.

WHAT THE INSTRUCTIONS ARE DEFENDING AGAINST
-------------------------------------------
Every numbered rule below exists because a vision model reliably gets that
specific thing wrong on architectural drawings:

  - Corners that nearly meet. The single most common defect. The grader snaps
    them, but a model told to reuse exact coordinates produces far fewer.
  - Openings measured from the wrong end, or from the centre instead of the
    near edge. Produces doors hanging off the end of walls.
  - Rooms invented from labels that are actually annotations, or missed because
    they are unlabelled. The survey pass fixes both by fixing the room list
    first.
  - Everything scaled from nothing. A model asked for meters will produce
    meters whether or not the drawing said anything about size, so scale is
    established explicitly and its evidence recorded.
"""
from __future__ import annotations

import json

# --------------------------------------------------------------------------
# Pass 1 — survey
# --------------------------------------------------------------------------
SURVEY_SYSTEM = """\
You are a chartered architectural technologist reading a floor plan in order to \
brief a modeller. You do not draw anything. You establish the facts the drawing \
states, and you are explicit about which facts it does not state.

Answer with a single JSON object and nothing else. No prose, no code fences."""

SURVEY_USER = """\
Read this floor plan and report what it actually shows.

Return exactly this JSON shape:

{
  "building_type": one of ["house","apartment","office","shop","restaurant",
                           "warehouse","school","clinic","hotel","mixed_use","other"],
  "building_type_reason": "one sentence on what in the drawing indicates this",
  "drawing_quality": "clear" | "usable" | "poor",
  "quality_notes": "what limits a confident reading, if anything",

  "scale": {
    "source": one of ["dimension_string","scale_bar","scale_ratio",
                      "room_label_area","door_heuristic","unknown"],
    "evidence": "quote the exact text or describe the marking you used",
    "overall_width_m": number or null,
    "overall_depth_m": number or null
  },

  "levels": [
    {"name": "Ground Floor", "shown_on_this_sheet": true}
  ],

  "rooms": [
    {"name": "as printed on the drawing", "approx_area_m2": number or null,
     "has_printed_area": true|false}
  ],

  "counts": {
    "exterior_doors": integer, "interior_doors": integer, "windows": integer
  },

  "features": {
    "has_stairs": bool, "has_lift": bool, "has_courtyard": bool,
    "has_curved_walls": bool, "has_angled_walls": bool,
    "notes": "anything a modeller must not miss"
  }
}

HOW TO ESTABLISH SCALE, in this order — stop at the first that applies:
1. A printed overall dimension (e.g. "20.00 m", "65'-0\"") along an edge.
   source = "dimension_string". Convert feet/inches to meters.
2. A scale bar. source = "scale_bar".
3. A scale ratio such as "1:100" combined with the sheet size. source = "scale_ratio".
4. A printed room area such as "Bedroom 14.2 m²". source = "room_label_area".
5. Nothing above: assume a single interior door leaf is 0.9 m wide and derive
   the rest from it. source = "door_heuristic".
6. Not even a door is identifiable: source = "unknown", dimensions null.

RULES
- Report only rooms the drawing shows. Do not add rooms a building "should" have.
- An unlabelled but clearly enclosed space IS a room. Name it by what it plainly
  is ("Corridor", "Store") and set has_printed_area false.
- A title block, legend, north arrow or key is not a room.
- If the sheet shows more than one storey side by side, list each in "levels".
- Never guess a number to look thorough. null is a valid, useful answer."""


# --------------------------------------------------------------------------
# Pass 2 — geometry
# --------------------------------------------------------------------------
GEOMETRY_SYSTEM = """\
You convert an architectural floor plan into precise 2D geometry for a BIM \
model. Accuracy of coordinates matters more than completeness of detail: a \
model with 12 correctly joined walls is useful, one with 40 walls that do not \
meet is not.

Answer with a single JSON object and nothing else. No prose, no code fences."""

GEOMETRY_USER_TEMPLATE = """\
Convert this floor plan into the BIM JSON contract below.

A survey of this same drawing has already been carried out. Treat it as
established fact and stay consistent with it — especially the scale, the
building type and the room list:

{survey_block}

COORDINATE SYSTEM
- Units: METERS. Never millimeters, never feet.
- Origin (0,0) at the bottom-left of the building's extent; +x right, +y up.
- Round every coordinate to 3 decimals (millimetre precision). Do not emit
  more; false precision is what stops corners from matching.

THE TEN RULES
1. SHARED CORNERS. Where two walls meet, both must use the IDENTICAL coordinate
   pair — the same digits. Trace the exterior outline as a closed loop first,
   reusing each corner, then add interior walls that terminate exactly on a
   point already used. This is the most important rule here.
2. Walls are CENTRELINES. `start` and `end` run along the middle of the wall,
   not along either face. `thickness` spreads evenly about that line.
3. Exterior walls are thicker than interior ones. Use what the drawing shows;
   where it shows nothing, {exterior_hint} m exterior and {interior_hint} m interior.
4. Every opening names its host with `wall_id`, never a position in a list.
5. `offset` is measured ALONG the host wall FROM ITS `start` POINT TO THE NEAR
   EDGE of the opening — not to its centre. `offset + width` must be less than
   the host wall's length. Check this arithmetic for every opening you emit.
6. Doors have `sill` 0. Windows have a sill above the floor, typically
   {sill_hint} m. Nothing may be taller than the wall holding it.
7. Room polygons are CLOSED and traced along the INNER faces of the surrounding
   walls. Do not repeat the first point at the end. Do not let two rooms
   overlap. Use the room names from the survey, exactly.
8. `outline` on each level is the building's exterior footprint as an ordered
   loop — including any L-shape, setback or courtyard edge. Never a bounding box.
9. IDs: walls W001, W002...; openings O001...; rooms R001...; levels L1, L2...;
   fixtures F001... Uppercase letters then digits. Every id unique.
10. OMIT WHAT YOU CANNOT SEE. A missing window costs nothing. An invented one
    is a defect the user has to find and delete.

RECORD YOUR ASSUMPTIONS
Every value you supplied that the drawing did not state goes in `assumptions`,
with `target` naming the field ("L1.wall_height", "W004.thickness") and `reason`
saying why. Wall heights, floor-to-floor and slab thickness are almost never
printed on a plan — assume them and say so.

FIXTURES
Emit `fixtures: []`. A separate pass reads the furniture from this same drawing
and does not need your help with it — spend your budget on getting the walls,
openings and rooms right.

THE CONTRACT

{example_block}

Field notes:
- `building_type`, `scale.source`, `scale.confidence` use the enum values shown.
- `wall.type`: "exterior" | "interior" | "partition" | "retaining".
- `wall.height`: null means "use the level's wall_height". Prefer null.
- `opening.type`: "door" | "double_door" | "sliding_door" | "window" | "opening"
  ("opening" is an unglazed, doorless hole — an archway or pass-through).
- `room.type` and `fixture.category` are free text.
- `confidence`: "measured" (read off the drawing), "inferred" (derived from
  something measured), "assumed" (a default).

Emit the complete object. Prefer fewer, correct walls over more, approximate ones."""


REPAIR_TEMPLATE = """\
Your previous answer was checked against the drawing and against geometric
rules. It scored {score}/100 and these problems were found:

{issues}

Produce the WHOLE JSON object again, corrected. Notes on the findings:
- "auto-fixed" means the system patched it for you. Do not repeat the mistake;
  the patch is a fallback, not an acceptable result.
- An opening that does not fit its wall means either the offset is measured
  from the wrong end, or the wall is the wrong one, or its length is wrong.
- Endpoints that had to be snapped mean you did not reuse the exact coordinates
  of shared corners. Fix the corners themselves, not the symptom.
- Rooms that overlap mean one polygon was traced along outer faces instead of
  inner faces.

Re-examine the drawing before answering. Do not simply resubmit the same
geometry with small edits."""


SCHEMA_REPAIR_TEMPLATE = """\
Your previous answer could not be used:

{error}

Return the complete, corrected JSON object. It must be one JSON object, valid,
with no code fences and no commentary. Do not truncate it — if you are running
long, shorten the `description` field, never the geometry."""


# --------------------------------------------------------------------------
# Pass 3 — furniture
# --------------------------------------------------------------------------
FURNITURE_SYSTEM = """\
You are taking off the furniture, fittings and equipment from an architectural \
floor plan. You read what is DRAWN and report where each item sits. You do not \
design layouts and you do not add anything the drawing does not show.

Answer with a single JSON object and nothing else. No prose, no code fences."""

FURNITURE_USER_TEMPLATE = """\
List every piece of furniture, sanitary ware and equipment drawn on this floor
plan.

The building's walls and rooms have already been traced. Use this room schedule
to place things — a fixture's coordinates must fall inside the room you assign
it to:

{rooms_block}

COORDINATE SYSTEM
- Units: METERS, same frame as the room polygons above: origin bottom-left,
  +x right, +y up. Round to 3 decimals.
- `position` is the fixture's CENTRE, not a corner.
- `size` is [width, depth] of its footprint, BEFORE rotation.
- `rotation` is degrees counter-clockwise. 0 means the item's width runs along
  +x. For a desk, rotate so its width runs along the edge it is pushed against;
  for a chair, so it faces its desk.

Return exactly:

{{
  "fixtures": [
    {{"id": "F001", "category": "desk", "position": [3.2, 8.4],
      "size": [1.6, 0.8], "height": 0.75, "rotation": 0.0, "room_id": "R001"}}
  ]
}}

CATEGORIES — use these words where they fit, free text where they do not:
  desk · workstation · chair · table · sofa · bed · counter · reception_desk
  cabinet · wardrobe · shelf · partition · wc · urinal · basin · sink · bath
  shower · fridge · stove · plant

THE RULES
1. COUNT EVERY ONE. An open-plan office drawn with thirty workstations gets
   thirty desks and thirty chairs, not one of each. Work through the drawing
   room by room and grid by grid so nothing is skipped.
2. Furniture drawn in a REPEATING pattern is still individual furniture. Read
   the spacing off the drawing and emit each item at its own coordinates.
3. A desk and the chair at it are TWO fixtures.
4. A run of toilet cubicles has one wc per cubicle. A vanity with three basins
   is three basins.
5. Do not invent. If a room is drawn empty, it stays empty.
6. Symbols you cannot identify still get a fixture, with your best guess at the
   category and the footprint you can see.
7. Typical heights, used unless the drawing says otherwise: desk and table 0.75,
   chair 0.9, counter 0.9, sofa 0.8, bed 0.5, wc 0.8, basin 0.85, urinal 1.1,
   cabinet 0.9, wardrobe 2.0, partition 1.4, shelf 1.8, plant 1.2.
8. `room_id` must be one of the ids in the schedule above. If an item sits in a
   corridor or in no room at all, use "".

Be exhaustive. A model missing half its furniture is the failure this pass
exists to prevent."""


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------
AUDIT_SYSTEM = """\
You are a quality checker comparing an extracted building model against the \
drawing it came from. You are looking for things that are WRONG or MISSING, \
not for things that could be more detailed.

Answer with a single JSON object and nothing else."""

AUDIT_USER_TEMPLATE = """\
Here is a summary of a building model that was extracted from the attached
floor plan:

{summary}

Compare it against the drawing and return:

{{
  "score": 0-100,
  "verdict": "one sentence",
  "mismatches": ["specific, checkable statements of what is wrong or missing"],
  "missed_rooms": ["room names visible on the drawing but absent from the model"],
  "phantom_rooms": ["room names in the model that are not on the drawing"],
  "scale_looks_wrong": true|false,
  "scale_comment": "if true, what the real overall size appears to be"
}}

SCORING
  90-100  matches the drawing; any differences are cosmetic
  70-89   sound overall; a few elements missing or slightly off
  50-69   recognisable but with real errors — wrong room count, wrong proportions
  25-49   loosely related to the drawing
   0-24   does not correspond to this drawing

Judge only what is checkable from the drawing. Wall heights, floor-to-floor and
material are not shown on a plan, so never penalise them. Room COUNT, room
NAMES, overall PROPORTIONS and the presence of major openings are checkable —
judge those."""


# --------------------------------------------------------------------------
# The worked example. Validated against BimPlan by the test suite.
# --------------------------------------------------------------------------
EXAMPLE_PLAN: dict = {
    "schema_version": "1.0",
    "units": "meters",
    "name": "Two-room unit",
    "building_type": "house",
    "description": (
        "A small rectangular unit, 8.0 m x 5.0 m, divided by one interior wall "
        "into a living room and a bedroom. Entrance door on the south wall, one "
        "window per room on the north wall."
    ),
    "scale": {
        "source": "dimension_string",
        "evidence": "Overall dimension '8.00 m' printed along the south elevation.",
        "confidence": "measured",
    },
    "levels": [
        {
            "id": "L1",
            "name": "Ground Floor",
            "elevation": 0.0,
            "floor_to_floor": 3.0,
            "wall_height": 2.7,
            "slab_thickness": 0.15,
            "outline": [[0.0, 0.0], [8.0, 0.0], [8.0, 5.0], [0.0, 5.0]],
        }
    ],
    "walls": [
        {
            "id": "W001", "start": [0.0, 0.0], "end": [8.0, 0.0],
            "thickness": 0.23, "type": "exterior", "height": None,
            "level_id": "L1", "load_bearing": True,
        },
        {
            "id": "W002", "start": [8.0, 0.0], "end": [8.0, 5.0],
            "thickness": 0.23, "type": "exterior", "height": None,
            "level_id": "L1", "load_bearing": True,
        },
        {
            "id": "W003", "start": [8.0, 5.0], "end": [0.0, 5.0],
            "thickness": 0.23, "type": "exterior", "height": None,
            "level_id": "L1", "load_bearing": True,
        },
        {
            "id": "W004", "start": [0.0, 5.0], "end": [0.0, 0.0],
            "thickness": 0.23, "type": "exterior", "height": None,
            "level_id": "L1", "load_bearing": True,
        },
        {
            "id": "W005", "start": [5.0, 0.0], "end": [5.0, 5.0],
            "thickness": 0.15, "type": "interior", "height": None,
            "level_id": "L1", "load_bearing": False,
        },
    ],
    "openings": [
        {
            "id": "O001", "type": "door", "wall_id": "W001",
            "offset": 1.8, "width": 0.9, "height": 2.1, "sill": 0.0,
        },
        {
            "id": "O002", "type": "door", "wall_id": "W005",
            "offset": 3.6, "width": 0.8, "height": 2.1, "sill": 0.0,
        },
        {
            "id": "O003", "type": "window", "wall_id": "W003",
            "offset": 1.2, "width": 1.5, "height": 1.2, "sill": 0.9,
        },
        {
            "id": "O004", "type": "window", "wall_id": "W003",
            "offset": 5.0, "width": 1.2, "height": 1.2, "sill": 0.9,
        },
    ],
    "rooms": [
        {
            "id": "R001", "name": "Living Room", "type": "living",
            "polygon": [[0.115, 0.115], [4.925, 0.115], [4.925, 4.885], [0.115, 4.885]],
            "level_id": "L1",
        },
        {
            "id": "R002", "name": "Bedroom", "type": "bedroom",
            "polygon": [[5.075, 0.115], [7.885, 0.115], [7.885, 4.885], [5.075, 4.885]],
            "level_id": "L1",
        },
    ],
    "fixtures": [
        {
            "id": "F001", "category": "bed", "position": [6.5, 3.5],
            "size": [1.5, 2.0], "height": 0.5, "rotation": 0.0,
            "room_id": "R002", "level_id": "L1",
        }
    ],
    "assumptions": [
        {
            "target": "L1.wall_height", "value": 2.7, "confidence": "assumed",
            "reason": "Not shown on a plan view; the residential default was used.",
        },
        {
            "target": "L1.slab_thickness", "value": 0.15, "confidence": "assumed",
            "reason": "Not shown on a plan view; the residential default was used.",
        },
    ],
}


def geometry_user_prompt(
    survey_json: str, *, exterior_hint: float, interior_hint: float, sill_hint: float
) -> str:
    return GEOMETRY_USER_TEMPLATE.format(
        survey_block=survey_json,
        example_block=json.dumps(EXAMPLE_PLAN, indent=2),
        exterior_hint=exterior_hint,
        interior_hint=interior_hint,
        sill_hint=sill_hint,
    )
