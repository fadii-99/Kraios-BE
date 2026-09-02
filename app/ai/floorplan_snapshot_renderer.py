"""Pillow FloorPlan snapshot renderer.

Step 2 prefers a frontend three.js WebGL snapshot uploaded as a THREE_D_SNAPSHOT
asset. When the client sends none, the Django adapter renders the camera
reference here from the extracted FloorPlan, and the fidelity auditor uses it to
eyeball the geometry against the original 2D plan.

The output reproduces the client viewer's capture contract rather than its
on-screen look: a 1536x864 (16:9) frame, white background, no grid, and the
viewer's own wall/floor colours, seen from a steep, mostly-overhead bird's-eye
angle (see `_axonometric`) chosen to read unambiguously in the render model's
output. The aspect ratio matters — when the snapshot's aspect differs from the
render model's canvas, the model re-frames the building to fill its own frame,
which is where long footprints get re-squared.
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from app.ai.floorplan_schema import FloorPlan

# The client captures on a fixed 16:9 buffer, white, with the grid hidden.
CANVAS_W = 1536
CANVAS_H = 864
# Fraction of the frame the model fills, mirroring the viewer's fit margin.
_FIT_MARGIN = 0.89

# Colours copied from the viewer's three.js materials so the massing reads the
# same to the render model: walls 0xcfd6e0, floor 0xcdc8bd.
_WALL_LIT = (207, 214, 224)
_WALL_SHADED = (175, 184, 198)
_WALL_TOP = (232, 236, 242)
_WALL_EDGE = (116, 128, 145)
_FLOOR = (205, 200, 189)
_FLOOR_EDGE = (146, 141, 130)
_ROOM_EDGE = (163, 158, 147)


def _axonometric(x: float, y: float, z: float) -> tuple[float, float]:
    """Steep oblique axonometric — a high, mostly-overhead bird's-eye angle.

    No frontend three.js snapshot has ever actually been uploaded in
    production (every THREE_D_SNAPSHOT asset on record was rendered here, by
    the fallback), so this is the sole camera reference the render model
    receives, and its exact framing is what the model is told to reproduce
    verbatim (see prompts.INPUTS_PREAMBLE). The previous, shallower angle
    (0.72, 0.38, 0.58) let walls rise tall enough to dominate the frame,
    leaving room for the model to drift toward a more side-on, less
    top-down-consistent interpretation from one generation to the next. This
    steeper set of coefficients shows far more floor per unit of wall height
    while still reading as a clearly 3D, non-flat view (never fully top-down —
    prompts.py explicitly forbids a flat/orthographic result).
    """
    return ((x - y) * 0.78, (x + y) * 0.50 - z * 0.34)


def _corners(plan: FloorPlan, wall_h: float) -> list[tuple[float, float, float]]:
    """Every geometry corner, at both floor and wall-top height."""
    points: list[tuple[float, float, float]] = []
    for x, y in plan.outline or []:
        points.append((x, y, 0.0))
    for wall in plan.walls:
        for x, y in (wall.start, wall.end):
            points.append((x, y, 0.0))
            points.append((x, y, wall_h))
    for room in plan.rooms:
        points.extend((x, y, 0.0) for x, y in room.polygon)
    return points


def _placer(plan: FloorPlan, wall_h: float):
    """Return a projector that centres and fills the capture frame."""
    projected = [_axonometric(*point) for point in _corners(plan, wall_h)]
    if not projected:
        return lambda x, y, z: (CANVAS_W / 2, CANVAS_H / 2)

    xs = [point[0] for point in projected]
    ys = [point[1] for point in projected]
    width = max(max(xs) - min(xs), 1e-6)
    height = max(max(ys) - min(ys), 1e-6)
    scale = min(CANVAS_W * _FIT_MARGIN / width, CANVAS_H * _FIT_MARGIN / height)
    offset_x = CANVAS_W / 2 - (min(xs) + max(xs)) / 2 * scale
    offset_y = CANVAS_H / 2 - (min(ys) + max(ys)) / 2 * scale

    def place(x: float, y: float, z: float) -> tuple[int, int]:
        axon_x, axon_y = _axonometric(x, y, z)
        return round(axon_x * scale + offset_x), round(axon_y * scale + offset_y)

    return place


def _wall_fill(wall) -> tuple[int, int, int]:
    """Two-tone shading so wall runs read as separate planes, as under the
    viewer's directional light."""
    along_x = abs(wall.end[0] - wall.start[0])
    along_y = abs(wall.end[1] - wall.start[1])
    return _WALL_LIT if along_x >= along_y else _WALL_SHADED


def _draw_snapshot(plan: FloorPlan) -> Image.Image:
    """Draw the open-top massing shell and return the PIL image (no disk I/O)."""
    wall_h = plan.wall_height or 2.7
    place = _placer(plan, wall_h)

    img = Image.new("RGB", (CANVAS_W, CANVAS_H), "#ffffff")
    draw = ImageDraw.Draw(img)

    floor_points = plan.outline if len(plan.outline) >= 3 else []
    if not floor_points and plan.rooms:
        floor_points = plan.rooms[0].polygon
    if floor_points:
        draw.polygon(
            [place(x, y, 0) for x, y in floor_points],
            fill=_FLOOR,
            outline=_FLOOR_EDGE,
        )

    for room in plan.rooms or []:
        if len(room.polygon) < 3:
            continue
        outline = [place(x, y, 0.01) for x, y in room.polygon]
        draw.line(outline + [outline[0]], fill=_ROOM_EDGE, width=2)

    # Painter's ordering: this projection puts larger (x + y) nearer the viewer,
    # so far walls are drawn first and near walls overlap them.
    walls = sorted(
        plan.walls,
        key=lambda wall: max(
            wall.start[0] + wall.start[1], wall.end[0] + wall.end[1]
        ),
    )
    for wall in walls:
        x1, y1 = wall.start
        x2, y2 = wall.end
        draw.polygon(
            [
                place(x1, y1, 0),
                place(x2, y2, 0),
                place(x2, y2, wall_h),
                place(x1, y1, wall_h),
            ],
            fill=_wall_fill(wall),
            outline=_WALL_EDGE,
        )
        draw.line(
            [place(x1, y1, wall_h), place(x2, y2, wall_h)],
            fill=_WALL_TOP,
            width=max(2, round((wall.thickness or 0.15) * 40)),
        )

    return img


def render_floorplan_snapshot_bytes(plan: FloorPlan) -> bytes:
    """Render the massing-shell snapshot straight to PNG bytes (no disk I/O).

    Used as the Step 2 camera reference when the client uploads no WebGL
    snapshot, and by the fidelity auditor to eyeball the extracted geometry
    against the original 2D plan.
    """
    import io

    buf = io.BytesIO()
    _draw_snapshot(plan).save(buf, format="PNG")
    return buf.getvalue()
