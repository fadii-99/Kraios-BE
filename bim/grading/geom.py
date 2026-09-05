"""Plane-geometry primitives, in pure Python and standard-library maths only.

WHY NOT SHAPELY
---------------
Everything here is a handful of lines against a few dozen points; the grader
runs in single-digit milliseconds on a realistic plan. Shapely would add a
compiled dependency to a package whose whole point is that it can be deleted
without consequence, in exchange for functions this module already has. If a
later phase needs real boolean operations (polygon clipping for room/outline
subtraction, say), that is the moment to reconsider — not before.

TOLERANCES
----------
Callers pass their own; the constants live in `checks.py` next to the rules
that use them, because a tolerance is a policy decision, not a geometric one.
Coordinates are meters throughout, so a tolerance of 0.02 means 2 cm.
"""
from __future__ import annotations

import math
from typing import Iterable, List, Sequence, Tuple

Point = Tuple[float, float]


def distance(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def bbox(points: Sequence[Point]) -> Tuple[float, float, float, float]:
    """(min_x, min_y, max_x, max_y). Raises on an empty sequence."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def bboxes_overlap(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def polygon_area(polygon: Sequence[Point]) -> float:
    """Unsigned area by the shoelace formula. 0.0 for degenerate input."""
    if len(polygon) < 3:
        return 0.0
    total = 0.0
    for index, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(index + 1) % len(polygon)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def polygon_perimeter(polygon: Sequence[Point]) -> float:
    if len(polygon) < 2:
        return 0.0
    return sum(
        distance(polygon[i], polygon[(i + 1) % len(polygon)])
        for i in range(len(polygon))
    )


def polygon_centroid(polygon: Sequence[Point]) -> Point:
    """Area centroid, falling back to the vertex mean for degenerate polygons.

    The fallback matters: a "polygon" that is really three collinear points has
    zero area and would divide by zero, and the callers that want a centroid
    (which room is this in, is this room inside the outline) still need an
    answer for it.
    """
    if not polygon:
        return (0.0, 0.0)
    if len(polygon) < 3:
        return (
            sum(p[0] for p in polygon) / len(polygon),
            sum(p[1] for p in polygon) / len(polygon),
        )

    signed_area = 0.0
    cx = 0.0
    cy = 0.0
    for index, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(index + 1) % len(polygon)]
        cross = x1 * y2 - x2 * y1
        signed_area += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross

    if abs(signed_area) < 1e-12:
        return (
            sum(p[0] for p in polygon) / len(polygon),
            sum(p[1] for p in polygon) / len(polygon),
        )

    signed_area /= 2.0
    return (cx / (6.0 * signed_area), cy / (6.0 * signed_area))


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """Ray-casting test. Points exactly on an edge may go either way.

    Edge cases are not worth chasing here: every caller uses this for a
    warning-level containment check where a boundary point is ambiguous anyway.
    """
    if len(polygon) < 3:
        return False
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def project_on_segment(point: Point, a: Point, b: Point) -> Tuple[float, Point]:
    """Return (t, closest_point) where t is the clamped 0..1 position along a→b."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-18:
        return 0.0, a
    t = ((point[0] - ax) * dx + (point[1] - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return t, (ax + t * dx, ay + t * dy)


def point_to_segment_distance(point: Point, a: Point, b: Point) -> float:
    _, closest = project_on_segment(point, a, b)
    return distance(point, closest)


def _orientation(a: Point, b: Point, c: Point) -> float:
    """Cross product of (b-a) x (c-a). >0 left turn, <0 right turn, 0 collinear."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(
    a1: Point, a2: Point, b1: Point, b2: Point, *, tolerance: float = 1e-9
) -> bool:
    """Proper intersection test, treating shared endpoints as NOT intersecting.

    Walls meeting at a corner share an endpoint by design, so an intersection
    test that flagged them would report every corner of every building.
    """
    shared_endpoint = any(
        distance(p, q) <= tolerance
        for p in (a1, a2)
        for q in (b1, b2)
    )
    if shared_endpoint:
        return False

    d1 = _orientation(b1, b2, a1)
    d2 = _orientation(b1, b2, a2)
    d3 = _orientation(a1, a2, b1)
    d4 = _orientation(a1, a2, b2)

    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    return False


def polygon_self_intersects(polygon: Sequence[Point], *, tolerance: float = 1e-9) -> bool:
    """True when any two non-adjacent edges cross.

    O(n²), which is fine: a room polygon that needs more than a few dozen
    vertices is already a data problem the grader flags separately.
    """
    count = len(polygon)
    if count < 4:
        return False
    for i in range(count):
        a1 = polygon[i]
        a2 = polygon[(i + 1) % count]
        for j in range(i + 1, count):
            # Skip adjacent edges (they legitimately share a vertex) and the
            # wrap-around pair of the first and last edge.
            if j == i or (j + 1) % count == i or (i + 1) % count == j:
                continue
            b1 = polygon[j]
            b2 = polygon[(j + 1) % count]
            if segments_intersect(a1, a2, b1, b2, tolerance=tolerance):
                return True
    return False


def collinear_overlap(
    a1: Point, a2: Point, b1: Point, b2: Point, *, tolerance: float
) -> float:
    """Length of the shared run of two near-collinear segments, else 0.0.

    Used to catch a wall drawn twice, or one wall drawn as two overlapping
    pieces — both produce z-fighting and doubled quantities downstream.
    """
    # Both endpoints of b must lie close to the infinite line through a, and
    # the two directions must be parallel, before an overlap means anything.
    if point_to_segment_distance(b1, a1, a2) > tolerance:
        return 0.0
    if point_to_segment_distance(b2, a1, a2) > tolerance:
        return 0.0

    length = distance(a1, a2)
    if length < 1e-9:
        return 0.0

    t_b1, _ = project_on_segment(b1, a1, a2)
    t_b2, _ = project_on_segment(b2, a1, a2)
    low, high = sorted((t_b1, t_b2))
    shared = max(0.0, min(1.0, high) - max(0.0, low))
    return shared * length


def polygons_overlap_ratio(
    polygon_a: Sequence[Point], polygon_b: Sequence[Point], *, samples: int = 24
) -> float:
    """Estimated fraction of the SMALLER polygon that lies inside the other.

    Grid sampling rather than exact clipping. This backs a warning-level check
    ("these two rooms overlap"), where an estimate within a few percent is
    indistinguishable in effect from an exact answer, and exact polygon
    clipping is several hundred lines that would then need their own tests.

    `samples` is per axis, so the default is up to 576 point-in-polygon tests
    per pair — still microseconds, and stable enough that a 5% threshold does
    not flicker between runs.
    """
    area_a = polygon_area(polygon_a)
    area_b = polygon_area(polygon_b)
    if area_a <= 0 or area_b <= 0:
        return 0.0

    smaller, larger = (polygon_a, polygon_b) if area_a <= area_b else (polygon_b, polygon_a)
    min_x, min_y, max_x, max_y = bbox(smaller)
    if max_x - min_x <= 0 or max_y - min_y <= 0:
        return 0.0

    inside_smaller = 0
    inside_both = 0
    step_x = (max_x - min_x) / samples
    step_y = (max_y - min_y) / samples
    for i in range(samples):
        x = min_x + (i + 0.5) * step_x
        for j in range(samples):
            y = min_y + (j + 0.5) * step_y
            if not point_in_polygon((x, y), smaller):
                continue
            inside_smaller += 1
            if point_in_polygon((x, y), larger):
                inside_both += 1

    if inside_smaller == 0:
        return 0.0
    return inside_both / inside_smaller


def convex_hull(points: Iterable[Point]) -> List[Point]:
    """Andrew's monotone chain. Returns counter-clockwise, without duplicates.

    Used only as a last-resort building outline when the extractor supplied
    none. A hull is wrong for any L-shaped or courtyard building, which is why
    it is a fallback that gets recorded as an assumption rather than a default.
    """
    unique = sorted(set((round(p[0], 6), round(p[1], 6)) for p in points))
    if len(unique) < 3:
        return list(unique)

    def build(sequence: List[Point]) -> List[Point]:
        chain: List[Point] = []
        for point in sequence:
            while len(chain) >= 2 and _orientation(chain[-2], chain[-1], point) <= 0:
                chain.pop()
            chain.append(point)
        return chain

    lower = build(unique)
    upper = build(list(reversed(unique)))
    return lower[:-1] + upper[:-1]
