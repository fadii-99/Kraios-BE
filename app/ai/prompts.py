"""Prompts copied from the developer Gemini/FloorPlan pipeline."""
from __future__ import annotations

SYSTEM_PROMPT = """\
You are a precise floor-plan vectorizer. You are given an image of a 2D architectural
floor plan. Your ONLY job is to output a JSON object describing its geometry. You do
NOT write code and you do NOT render anything — you only measure and describe.

Coordinate system:
- Units are METERS.
- Origin (0,0) is the bottom-left of the plan.
- +x points right, +y points up (as the plan is drawn).

Scale:
- If the drawing has dimension annotations, a scale bar, or a stated scale, use them
  to set real-world meters. Briefly explain what you used in "scale_note".
- If no scale information exists, estimate from typical room sizes (e.g. a bedroom is
  ~3-4 m wide, an interior door ~0.8-0.9 m, a hallway ~1-1.5 m) and clearly state
  this assumption in "scale_note".

Ignore everything that is NOT the building itself. Real CAD sheets contain a lot
of non-architectural content — do not treat any of it as walls:
- the sheet border / drawing frame and the outer rectangle of the page,
- the title block and all of its text (names, references, logos, dates),
- legends, scale bars, north arrows, revision/index tables,
- dimension lines, extension/leader lines and dimension text,
- hatching and fill patterns (e.g. tiled or seating areas),
- any standalone text or annotations.
Trace ONLY the actual architectural walls of the building. Walls may run at
arbitrary angles (not only horizontal/vertical) — follow the real drawn wall
lines, including diagonal ones.

Accuracy first — the JSON must reproduce the drawing as faithfully as possible:
- Keep everything strictly TO SCALE: preserve the true proportions and overall
  aspect ratio of the plan. A space that is twice as long as another must be
  twice as long in your coordinates.
- Be COMPLETE: trace every exterior wall AND every interior partition. Do not
  simplify, merge or skip walls. Prefer more, accurate segments over fewer.
- Be EXACT about angles: right angles where the drawing is orthogonal; the real
  angle where walls are diagonal/slanted or curved (approximate curves with
  several short segments).
- Be WATERTIGHT: where two walls meet at a corner, their endpoints must use the
  EXACT same [x,y] so the shell closes with no gaps or overshoots.

Rules:
- "outline": the building's overall exterior footprint as ONE ordered polygon of
  its outer corner points (follow the outermost walls all the way around,
  including every diagonal/angled edge). This defines the floor slab, so it must
  match the real footprint shape closely.
- "walls" is a list of straight wall segments. Each has "start":[x,y], "end":[x,y],
  and "thickness" in meters (default 0.15 if unknown). Trace exterior and interior
  walls. Connect walls at shared corners so the layout is closed where the plan is.
- "openings" are doors and windows. For each, "wall_index" is the index into "walls"
  of the wall it sits on; "offset" is the distance in meters along that wall measured
  from its "start" to the opening's near edge; "width" and "height" in meters; for
  windows also give "sill" (height of the sill above the floor); doors use sill 0.
- "rooms": for each enclosed space give a "name" (best guess from labels, else
  "Room 1", etc.) and a "polygon" as an ordered list of [x,y] corner points.
  Rooms together should cover the interior with no gaps or overlaps.
- "wall_height": a single default storey height in meters (use 2.7 if unknown).
- "description": a thorough natural-language description of the plan, written so
  someone could reconstruct it in 3D without seeing the drawing. Cover: the
  building type/purpose; the overall footprint and outline shape (note diagonal
  or angled walls); every room/space with its approximate size and what it is;
  how spaces connect (adjacencies, circulation, the main entrance); where doors
  and windows are; stairs/level changes; and any fixed fixtures or notable
  architectural features visible on the drawing. Be specific and spatial, but
  keep it under 250 words — geometry lives in the arrays, not here. Do NOT
  describe the sheet border, title block, dimension lines or annotations.

Hard validation limits — your JSON is machine-validated and the WHOLE response
is rejected if ANY rule below is violated, so verify each one before you answer:
- Every number is in METERS. Sanity ranges: wall "thickness" > 0 and <= 2.0
  (typically 0.1-0.4); opening "width" > 0 and <= 20; opening "height" > 0 and
  <= 6; window "sill" >= 0 and <= 4 (doors: 0); "wall_height" > 0 and <= 10.
  If you are about to write a value like 15 or 230 for a thickness or height,
  you have slipped into centimeters — convert it.
- No zero-length walls: every wall's "start" must differ from its "end".
- "walls" must contain at least 1 wall.
- Every room needs a non-empty "name" and a "polygon" of at least 3 points.
- Every openings[].wall_index must be an integer from 0 to walls.length - 1 and
  must point at the wall that opening actually sits on. AFTER writing the
  openings, re-count your walls array and re-check every wall_index is in range.
- Each opening's "offset" must fit on its wall: offset + width should not exceed
  that wall's length.
- Strict JSON syntax: double quotes only, no trailing commas, no comments, no
  NaN/Infinity.

Output ONLY a single JSON object. No markdown, no prose, no code fences.

JSON schema (shape, with example values):
{
  "units": "meters",
  "wall_height": 2.7,
  "scale_note": "Derived from the 5.00 m dimension on the south wall.",
  "outline": [[0,0],[8,0],[8,6],[0,6]],
  "walls": [
    {"start": [0, 0], "end": [8, 0], "thickness": 0.2},
    {"start": [8, 0], "end": [8, 6], "thickness": 0.2}
  ],
  "openings": [
    {"type": "door", "wall_index": 0, "offset": 1.2, "width": 0.9, "height": 2.1, "sill": 0},
    {"type": "window", "wall_index": 1, "offset": 2.0, "width": 1.5, "height": 1.2, "sill": 0.9}
  ],
  "rooms": [
    {"name": "Living Room", "polygon": [[0,0],[8,0],[8,6],[0,6]]}
  ],
  "description": "Single-storey rectangular dwelling, ~8 m x 6 m. One open living room occupying the full footprint, entered by a door on the south wall near the south-west corner, with a window centred on the east wall ..."
}
"""

USER_PROMPT = (
    "Analyze this floor plan and return the JSON object described in the system "
    "message. Output JSON only."
)

REPAIR_PROMPT_TEMPLATE = """\
Your previous response did not pass schema validation. Fix it and return ONLY the
corrected JSON object (no prose, no code fences).

Validation error:
{error}

Your previous output:
{previous}
"""

ARCHITECT_2D_PROMPT_TEMPLATE = """\
[TASK]
Generate a professional, high-precision 2D architectural floor plan
(engineering blueprint). do not make graph tables in background

[GEOMETRY REQUIRED]
{description}

[VIEWPORT & STYLE SETTINGS]
- Perspective: strict top-down orthographic view.
- Style: traditional CAD blueprint schematic with high contrast.
- Color palette: monochrome black linework on a clean white background.

[ARCHITECTURAL DETAILS & PRECISION]
- Use thick, solid linework for structural cut walls and thinner linework for
  furniture, fixtures, doors, and annotations.
- Show doors with swing arcs and show window openings clearly in walls.
- Use standard architectural hatching inside cut walls where helpful.
- Include a subtle 1 m x 1 m grid, edge ruler, or another clear scale cue.
- Include explicit dimension lines with text for the main exterior and interior
  spans.

[OUTPUT REQUIREMENTS]
- Include clear room labels, such as KITCHEN, BEDROOM, BATH, and LIVING.
- Keep furniture schematic/outline-only so it does not obscure structural lines.
- Return a clean, high-resolution floor-plan image suitable for visual review
  and downstream computer vision analysis.
- do not make graph tables in background
"""

# Used by /api/render. Gemini receives, IN THIS ORDER:
#   1. the 3D massing snapshot  -> camera / 3D viewpoint only
#   2. the original 2D CAD plan -> AUTHORITATIVE layout (faithful reproduction)
#   3+. optional style references -> style only
# plus the text description below. Templates share {rooms}, {style},
# {plan_description}, {ref_clause}.
INPUTS_PREAMBLE = (
    "You are given multiple images IN ORDER:\n"
    "- IMAGE 1 is a flat-shaded 3D model of the building, ALREADY CORRECTLY "
    "BUILT from the CAD plan: every wall, room, door and window opening is in "
    "its TRUE position and proportion. It is the SOLE authority for BOTH the "
    "GEOMETRY/LAYOUT and the CAMERA. Keep its exact viewpoint — the oblique "
    "elevated three-quarter bird's-eye angle, the downward tilt, the "
    "perspective and the framing — and its open-top cut-away (no roof) "
    "convention.\n"
    "- IMAGE 2 is the ORIGINAL 2D CAD floor plan that IMAGE 1 was built from. "
    "Use it ONLY as a secondary cross-check for fine details: door/window "
    "positions, printed dimension figures and each room's purpose. IMAGE 2 has "
    "NO camera — never adopt its flat top-down orientation or use it as a "
    "viewpoint. If IMAGE 1 and IMAGE 2 ever appear to disagree, KEEP IMAGE 1's "
    "geometry — do NOT redesign the building from the drawing.\n"
    "- IMAGE 2 is a 2D drafting document and contains 2D-ONLY drafting "
    "symbols that DO NOT physically exist in the real building and must NEVER "
    "be reproduced as marks in the photorealistic 3D output: door-swing arcs "
    "(the curved quarter-circle lines showing how a door swings), dimension "
    "lines, extension/leader lines, tick marks, hatching, grid lines and any "
    "printed text or labels. Use these symbols only to read door/window "
    "positions and dimensions — then render an ordinary clean 3D floor with "
    "NO curved lines, NO tick marks and NO hatching anywhere on it. A door in "
    "the output must be a real 3D door (a panel in or beside its frame), never "
    "a flat arc or line drawn on the floor.\n"
    "Your task is to REPAINT the exact model shown in IMAGE 1 — same "
    "footprint, same walls, same openings, same room sizes, rendered from the "
    "same camera — applying ONLY the materials, furnishing and lighting "
    "described in the style instructions below. Do NOT "
    "move, add, remove, resize, merge or rearrange any wall, room, door or "
    "window. The result must clearly be an oblique 3D view with visible wall "
    "heights and interior depth — NOT a top-down, plan-aligned, orthographic "
    "or flat view."
)

# Reinforced as the final instruction (recency) in every render template.
_CAMERA_DIRECTIVE = (
    "CAMERA (critical): render from the EXACT same oblique, elevated "
    "three-quarter viewpoint, tilt and framing as IMAGE 1, and keep IMAGE 1's "
    "geometry unchanged. The flat 2D CAD plan must NEVER influence the camera; "
    "do not produce a top-down or plan-aligned image.\n\n"
)

# Appended when the user attaches style reference image(s) (after the CAD plan).
REFERENCE_CLAUSE = (
    "Any further images after the 2D CAD plan are STYLE references only — match "
    "their line work, palette, materials, furniture style, greenery, lighting "
    "and background, but NEVER take layout from them.\n\n"
)

_PLAN_BLOCK = """

Written description of the building (it matches the layout already built in
IMAGE 1; use it to understand each room's PURPOSE and to resolve ambiguity —
never to change IMAGE 1's geometry):
{plan_description}

Rooms/spaces: {rooms}.
{geometry_facts}"""

# Reinforced right before the camera directive (recency) in every render
# template — models otherwise tend to copy IMAGE 2's raw drafting linework
# (door-swing arcs especially) onto the floor of the photoreal/sketchup output.
_NO_DRAFTING_SYMBOLS_BLOCK = """
ZERO TOLERANCE — NO 2D DRAFTING SYMBOLS IN THE 3D OUTPUT:
- The curved quarter-circle arc line next to a door in IMAGE 2 is a 2D
  drafting symbol for "door swing direction". It is NOT a physical object and
  must NEVER appear as a line, arc, or mark on the floor in your render.
- Before finishing, scan every doorway you drew: if the floor near it has any
  curved line, semicircle, arc or sweep mark, remove it and redraw a clean
  floor with an ordinary 3D door (a panel at the opening) instead.
- The same applies to any other 2D-only markings from IMAGE 2 — dimension
  lines, tick marks, leader lines, hatching, grid lines: none of these may be
  copied onto the 3D floor or walls. (The dimension labels you DO add are the
  clean callouts described below — not a copy of IMAGE 2's drafting lines.)
"""

# Dimension labelling: Gemini draws the room dimensions directly on the render,
# reading the numbers from the original CAD document (IMAGE 2) as the primary
# source. Shared by every render template so labels are consistent.
_DIMENSION_LABEL_BLOCK = """
DIMENSION LABELS (required — draw these on the render):
- Label every room/space with its size as a small, clean dimension callout
  placed inside (or directly over) that room, e.g. "3.4 × 4.3 m" with the area
  "14.6 m²" on a second line.
- The ORIGINAL 2D CAD plan (IMAGE 2) is the PRIMARY, authoritative source of all
  dimensions: read the printed dimension figures and the scale from IMAGE 2 and
  copy those exact numbers onto the matching room. If a room's size is not
  printed, derive it from the CAD plan's scale and proportions — never invent
  arbitrary numbers.
- One label per room, kept short and legible: dimensions and area ONLY. Do NOT
  write room names, function names, titles, legends, or any other text.
- Render each label as small dark text (optionally on a subtle light pill) so it
  stays readable against the floor/furniture, and keep it upright and flat-on
  to the camera so it is easy to read.
- Place each label so it sits clearly within its own room and does not overlap
  neighbouring labels.
"""

PHOTOREAL_PROMPT_TEMPLATE = (
    INPUTS_PREAMBLE
    + _PLAN_BLOCK
    + """
Render it as a single photorealistic architectural visualization:
- LAYOUT FIDELITY IS THE TOP PRIORITY — IMAGE 1 already shows the correct
  building; REPAINT it exactly, overriding any style reference for everything
  except materials/furnishing:
    * Keep the overall footprint/outline shape and its proportions and aspect
      ratio exactly as IMAGE 1. Do NOT stretch, rotate, mirror, skew or
      re-square it.
    * Keep EVERY exterior wall and EVERY interior partition exactly where
      IMAGE 1 has them, with the same door and window openings.
    * Keep the EXACT same set of rooms — same COUNT, same relative positions,
      sizes and adjacencies as IMAGE 1. Do NOT add, remove, merge, split,
      duplicate, resize or rearrange any room, wall, door or window, and do NOT
      invent rooms or spaces that are not in IMAGE 1.
    * Each room's size relative to the others must stay exactly as in IMAGE 1.
  Before finalizing, verify the room count and arrangement against IMAGE 1 and
  cross-check the openings against IMAGE 2.
- Use IMAGE 2 only to understand each room's purpose and its printed dimension
  figures — never to redesign the geometry.
- Furnish each room realistically and tastefully for its stated purpose
  (you decide the furniture; keep it appropriate to each space).
- {style} interior design. Realistic materials (wood, fabric, tile, glass),
  soft natural daylight, accurate shadows, subtle ambient occlusion.
- COMPLETE & CLEAN: render the ENTIRE building footprint fully finished. Every
  room must have a complete, evenly-lit floor with furniture — NO black or empty
  voids, NO dark unrendered patches, NO missing or cut-out floor areas inside
  the building. The area OUTSIDE the footprint must be a clean, seamless solid
  ***Important: WHITE background — never black, and never filled with dark terraces, voids or
  filler geometry.***
- EXPOSURE: balanced, neutral exposure like a properly-metered architectural
  photo. Do NOT overexpose or blow out the image; keep full detail in walls,
  floors and bright surfaces. No washed-out whites, no glaring highlights, no
  hazy white bloom, no foggy over-bright look. Mid-tones and whites must stay
  clearly readable with natural contrast and neutral white balance.
- MATERIAL / SURFACE CONTINUITY — mandatory acceptance check:
    * Treat every floor, wall and other finish as one coherent physical surface.
      Keep its base colour, material identity, texture scale and grain direction
      stable until a deliberate architectural material boundary is reached.
    * Natural fine variation is allowed, but it must stay subtle and continuous;
      never turn into distressed paint, procedural grunge, high-contrast noise,
      eroded coverage, or a broken/unfinished texture.
    * There must be ZERO white/transparent/erased holes, washed-out islands,
      missing-material patches, checkerboarding, speckles, stippling,
      salt-and-pepper dots, or random bright/dark blotches inside any surface.
    * Lighting and highlights must never look like missing floor/wall material.
      If any surface becomes locally white, patchy, noisy or discontinuous,
      reject that attempt and render it again with continuous material coverage.
- Architectural-photography quality. No watermarks and no people. The only text
  allowed is the dimension labels described below — no room names or titles.
"""
    + _NO_DRAFTING_SYMBOLS_BLOCK
    + _DIMENSION_LABEL_BLOCK
    + """
"""
    + _CAMERA_DIRECTIVE
    + "{ref_clause}Return only the rendered image.\n"
)


SKETCHUP_PROMPT_TEMPLATE = (
    INPUTS_PREAMBLE
    + _PLAN_BLOCK
    + """
Render it as a polished SketchUp architectural model — a clean, professional
3D architectural render (like a well-made SketchUp scene), NOT a loose hand
sketch:
- The layout MUST faithfully match the 3D model in IMAGE 1 (already correctly
  built from the CAD plan): same outline (including angled/diagonal walls),
  same wall positions, room shapes, proportions and door/window openings.
  Repainting IMAGE 1 without moving ANY geometry is the single most important
  requirement; use IMAGE 2 and the description only to understand each room's
  purpose and details.
- Open-top cut-away dollhouse (no roof/ceiling) seen from the SAME oblique
  elevated 3/4 viewpoint as IMAGE 1.
- Equip every space with the elements that match its REAL function from the
  CAD plan and the description — NOT generic living-room furniture. Examples:
  a cinema/auditorium gets straight, evenly spaced rows of tiered theatre
  seats all facing the screen wall; a bar/kitchenette gets a counter with
  stools and cabinetry/appliances; a lounge gets sofas and low tables; a
  corridor/vestibule stays mostly clear, with stairs where indicated.
  Reproduce repetitive arrangements (seating grids, rows) faithfully in count,
  spacing and alignment as the description specifies.
- Clean matte white / warm off-white walls; smooth light floors; realistic but
  understated SketchUp materials (wood, fabric, metal). Crisp straight edge
  lines, smooth even shading, soft ambient occlusion, soft natural daylight.
- Where there are level edges, stairs or mezzanines, add slim metal-framed
  balustrades with translucent blue-tinted glass panels.
- Tasteful greenery only where it genuinely suits the space.
- Overlay clean architectural DIMENSION ANNOTATIONS: thin dimension lines with
  small arrowheads (or tick marks) at each end, running alongside the major
  exterior walls and the overall footprint width and depth, plus a few key
  interior room spans. Label each with its measurement in meters (e.g.
  "4.20 m") in a small, neutral grey technical font. Place them tidily just
  outside or along the base of the relevant walls on the floor plane so they
  stay legible and never clutter the model, hide furniture, or cross the walls.
  Estimate the measurements from the plan's proportions and scale; keep them
  internally consistent (larger spaces read as larger numbers).
- Seamless pure-white background, soft contact shadows. {style} feel.
- keep it very true to typical sketchup style: no photorealism, no heavy
  textures, no watermarks. The only text allowed is the dimension measurements
  described above — no room names or titles.
"""
    + _NO_DRAFTING_SYMBOLS_BLOCK
    + """
"""
    + _CAMERA_DIRECTIVE
    + "{ref_clause}Return only the rendered image.\n"
)

DEFAULT_STYLE = "Modern, warm, neutral-palette,sketchup style"

# mode -> (prompt template, default style). Consumed by main.py /api/render.
RENDER_MODES = {
    "photoreal": (PHOTOREAL_PROMPT_TEMPLATE, "Modern, warm, neutral-palette"),
    "sketchup": (
        SKETCHUP_PROMPT_TEMPLATE,
        "Clean professional architectural model: crisp white / warm-neutral "
        "walls, light floors, understated realistic materials,sketchup style lighting and shading, tasteful greenery only where it suits the space.",
    ),
}

# Used by /api/refine: a feedback loop that corrects the FloorPlan geometry by
# comparing the current 3D shell against the CAD and the SketchUp render.
# System prompt stays SYSTEM_PROMPT (same schema + accuracy rules).
REFINE_USER_PROMPT_TEMPLATE = """\
You are correcting a 3D reconstruction. You are given these images, in order:
{image_legend}

Compare IMAGE 1 against the reference image(s), then output a CORRECTED FloorPlan
JSON (the exact same schema and coordinate system as specified) so that
rebuilding the 3D shell from it matches the real building far better. Focus on:
- the "outline" footprint shape and proportions (match IMAGE 2),
- missing, extra or misplaced "walls" and interior partitions,
- "rooms" polygons so they tile the interior with no gaps/overlaps,
- correct overall scale and aspect ratio,
- watertight corners (shared endpoints use identical [x,y]).
Preserve anything already correct. Output ONLY the corrected JSON object.

Current FloorPlan JSON:
{current_json}
"""

# Image legends for the refine prompt (snapshot is always IMAGE 1).
REFINE_LEGEND_WITH_CAD = (
    "- IMAGE 1: a rough 3D massing snapshot built from the JSON below — the\n"
    "  CURRENT result, known to be inaccurate.\n"
    "- IMAGE 2: the ORIGINAL 2D CAD floor plan — authoritative ground truth for\n"
    "  layout, outline, walls, rooms, proportions and scale.\n"
    "- IMAGE 3: a high-quality 3D SketchUp render of the same building — a\n"
    "  faithful 3D interpretation of how the spaces read in 3D."
)
REFINE_LEGEND_NO_CAD = (
    "- IMAGE 1: a rough 3D massing snapshot built from the JSON below — the\n"
    "  CURRENT result, known to be inaccurate.\n"
    "- IMAGE 2: a high-quality 3D SketchUp render of the same building — a\n"
    "  faithful 3D interpretation; treat it as the layout reference."
)


def build_render_prompt(
    *,
    mode: str,
    rooms: str,
    style: str,
    description: str,
    has_refs: bool,
    geometry_facts: str = "",
) -> str:
    preset = RENDER_MODES.get(mode)
    if preset is None:
        raise ValueError(
            f"Unknown render mode '{mode}'. Use one of: {', '.join(RENDER_MODES)}."
        )
    template, default_style = preset
    room_list = ", ".join(r.strip() for r in rooms.split(",") if r.strip())
    plan_desc = description.strip() or "(no written description available)"
    return template.format(
        rooms=room_list or "the rooms shown",
        style=style.strip() or default_style,
        plan_description=plan_desc,
        ref_clause=REFERENCE_CLAUSE if has_refs else "",
        geometry_facts=geometry_facts,
    )
