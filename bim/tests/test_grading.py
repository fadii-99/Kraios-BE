"""Grader tests.

Each case builds the smallest plan that exhibits one defect, so a failure names
the rule that broke rather than "the grader is unhappy about something". No
database and no AI provider is involved — `SimpleTestCase` on purpose.
"""
from django.test import SimpleTestCase

from bim.grading import grade
from bim.grading import geom
from bim.schema import (
    BimPlan,
    BuildingType,
    Level,
    Opening,
    OpeningType,
    Room,
    Scale,
    ScaleSource,
    Wall,
    WallType,
)


def _square_walls(size=10.0, level_id="L1"):
    """Four walls forming a closed square, corners exactly shared."""
    corners = [(0.0, 0.0), (size, 0.0), (size, size), (0.0, size)]
    return [
        Wall(
            id=f"W{index + 1:03d}",
            start=corners[index],
            end=corners[(index + 1) % 4],
            thickness=0.23,
            type=WallType.EXTERIOR,
            level_id=level_id,
        )
        for index in range(4)
    ]


def _plan(**overrides):
    size = overrides.pop("size", 10.0)
    base = {
        "building_type": BuildingType.HOUSE,
        "scale": Scale(source=ScaleSource.DIMENSION_STRING, evidence="10.00 m"),
        "levels": [
            Level(
                id="L1",
                name="Ground Floor",
                outline=[(0.0, 0.0), (size, 0.0), (size, size), (0.0, size)],
            )
        ],
        "walls": _square_walls(size),
        "openings": [
            Opening(
                id="O001",
                type=OpeningType.DOOR,
                wall_id="W001",
                offset=4.0,
                width=0.9,
                height=2.1,
            )
        ],
        "rooms": [
            Room(
                id="R001",
                name="Living Room",
                polygon=[(0.1, 0.1), (size - 0.1, 0.1), (size - 0.1, size - 0.1), (0.1, size - 0.1)],
            )
        ],
    }
    base.update(overrides)
    return BimPlan(**base)


def _codes(report):
    return {issue.code for issue in report.issues}


class CleanPlanTests(SimpleTestCase):
    def test_a_well_formed_plan_grades_highly_and_is_accepted(self):
        _, report = grade(_plan())

        self.assertEqual(report.unrepaired_errors, [])
        self.assertGreaterEqual(report.score, 90)
        self.assertEqual(report.grade, "A")
        self.assertTrue(report.is_acceptable())

    def test_stats_describe_the_plan(self):
        _, report = grade(_plan())

        self.assertEqual(report.stats["walls"], 4)
        self.assertEqual(report.stats["doors"], 1)
        self.assertEqual(report.stats["rooms"], 1)
        self.assertAlmostEqual(report.stats["footprint_m2"], 100.0, places=1)

    def test_the_input_plan_is_never_mutated(self):
        plan = _plan()
        plan.walls[1].start = (10.004, 0.0)  # a near miss the grader will snap

        repaired, _ = grade(plan)

        self.assertEqual(plan.walls[1].start, (10.004, 0.0))
        self.assertEqual(repaired.walls[1].start, (10.0, 0.0))


class WallRepairTests(SimpleTestCase):
    def test_near_miss_endpoints_are_snapped_together(self):
        plan = _plan()
        plan.walls[1].start = (10.02, 0.01)

        repaired, report = grade(plan)

        self.assertIn("WALL_ENDPOINT_SNAPPED", _codes(report))
        self.assertEqual(repaired.walls[1].start, (10.0, 0.0))

    def test_endpoints_further_apart_than_the_tolerance_are_left_alone(self):
        plan = _plan()
        plan.walls[1].start = (10.4, 0.0)

        repaired, report = grade(plan)

        self.assertNotIn("WALL_ENDPOINT_SNAPPED", _codes(report))
        self.assertEqual(repaired.walls[1].start, (10.4, 0.0))

    def test_a_degenerate_wall_is_removed_with_its_openings(self):
        plan = _plan()
        plan.walls.append(
            Wall(id="W099", start=(2.0, 2.0), end=(2.01, 2.0), level_id="L1")
        )
        plan.openings.append(
            Opening(
                id="O099",
                type=OpeningType.WINDOW,
                wall_id="W099",
                offset=0.0,
                width=0.005,
                height=1.2,
                sill=0.9,
            )
        )

        repaired, report = grade(plan)

        self.assertIn("WALL_DEGENERATE", _codes(report))
        self.assertIn("OPENING_ORPHANED", _codes(report))
        self.assertNotIn("W099", [w.id for w in repaired.walls])
        self.assertNotIn("O099", [o.id for o in repaired.openings])

    def test_a_duplicated_wall_is_removed_and_its_openings_rehosted(self):
        plan = _plan()
        plan.walls.append(
            Wall(id="W100", start=(0.0, 0.0), end=(10.0, 0.0), level_id="L1")
        )
        plan.openings.append(
            Opening(
                id="O100",
                type=OpeningType.WINDOW,
                wall_id="W100",
                offset=1.0,
                width=1.2,
                height=1.2,
                sill=0.9,
            )
        )

        repaired, report = grade(plan)

        self.assertIn("WALL_DUPLICATE", _codes(report))
        self.assertNotIn("W100", [w.id for w in repaired.walls])
        rehosted = next(o for o in repaired.openings if o.id == "O100")
        self.assertEqual(rehosted.wall_id, "W001")

    def test_a_reversed_duplicate_mirrors_its_openings_onto_the_survivor(self):
        """W001 runs 0->10; its twin runs 10->0, so offsets are measured from
        the other end and must be mirrored, not merely re-hosted."""
        plan = _plan()
        plan.walls.append(
            Wall(id="W100", start=(10.0, 0.0), end=(0.0, 0.0), level_id="L1")
        )
        plan.openings.append(
            Opening(
                id="O100",
                type=OpeningType.WINDOW,
                wall_id="W100",
                offset=1.0,  # 1 m from the twin's start == 9 m from W001's start
                width=1.2,
                height=1.2,
                sill=0.9,
            )
        )

        repaired, report = grade(plan)

        self.assertIn("WALL_DUPLICATE", _codes(report))
        moved = next(o for o in repaired.openings if o.id == "O100")
        self.assertEqual(moved.wall_id, "W001")
        self.assertAlmostEqual(moved.offset, 7.8)  # 10 - 1.0 - 1.2

    def test_a_same_direction_duplicate_keeps_its_offsets(self):
        plan = _plan()
        plan.walls.append(
            Wall(id="W100", start=(0.0, 0.0), end=(10.0, 0.0), level_id="L1")
        )
        plan.openings.append(
            Opening(
                id="O100",
                type=OpeningType.WINDOW,
                wall_id="W100",
                offset=1.0,
                width=1.2,
                height=1.2,
                sill=0.9,
            )
        )

        repaired, _ = grade(plan)

        moved = next(o for o in repaired.openings if o.id == "O100")
        self.assertEqual(moved.wall_id, "W001")
        self.assertAlmostEqual(moved.offset, 1.0)

    def test_implausible_thickness_is_clamped_to_the_type_default(self):
        plan = _plan()
        plan.walls[0].thickness = 9.0
        plan.walls[0].type = WallType.EXTERIOR

        repaired, report = grade(plan)

        self.assertIn("WALL_THICKNESS_IMPLAUSIBLE", _codes(report))
        self.assertAlmostEqual(repaired.walls[0].thickness, 0.23)
        self.assertTrue(
            any(a.target == "W001.thickness" for a in repaired.assumptions),
            "a clamped thickness must be recorded as an assumption",
        )

    def test_disconnected_walls_are_reported(self):
        plan = _plan()
        plan.walls = [
            Wall(id="W001", start=(0.0, 0.0), end=(5.0, 0.0), level_id="L1"),
            Wall(id="W002", start=(20.0, 20.0), end=(25.0, 20.0), level_id="L1"),
        ]
        plan.openings = []

        _, report = grade(plan)

        self.assertIn("WALL_ENDS_DANGLING", _codes(report))


class OpeningRepairTests(SimpleTestCase):
    def test_an_opening_running_past_the_end_of_its_wall_is_moved_back(self):
        plan = _plan()
        plan.openings[0].offset = 9.8  # 0.9 m wide door on a 10 m wall

        repaired, report = grade(plan)

        self.assertIn("OPENING_OUT_OF_BOUNDS", _codes(report))
        door = repaired.openings[0]
        self.assertLessEqual(door.offset + door.width, 10.0)

    def test_an_opening_wider_than_its_wall_is_removed(self):
        plan = _plan()
        plan.openings[0].width = 14.0

        repaired, report = grade(plan)

        self.assertIn("OPENING_WIDER_THAN_WALL", _codes(report))
        self.assertEqual(repaired.openings, [])

    def test_an_opening_taller_than_its_wall_is_shortened(self):
        plan = _plan()
        plan.openings[0].height = 5.0  # wall height defaults to 2.7

        repaired, report = grade(plan)

        self.assertIn("OPENING_TALLER_THAN_WALL", _codes(report))
        door = repaired.openings[0]
        self.assertLessEqual(door.sill + door.height, 2.7)

    def test_overlapping_openings_lose_the_narrower_one(self):
        plan = _plan()
        plan.openings.append(
            Opening(
                id="O002",
                type=OpeningType.DOOR,
                wall_id="W001",
                offset=4.2,
                width=0.7,
                height=2.1,
            )
        )

        repaired, report = grade(plan)

        self.assertIn("OPENING_OVERLAP", _codes(report))
        self.assertEqual([o.id for o in repaired.openings], ["O001"])

    def test_a_door_never_keeps_a_sill(self):
        door = Opening(
            id="O001", type=OpeningType.DOOR, wall_id="W001", offset=1.0,
            width=0.9, height=2.1, sill=0.9,
        )
        self.assertEqual(door.sill, 0.0)

    def test_an_implausibly_wide_door_is_warned_about_but_kept(self):
        plan = _plan()
        plan.openings[0].width = 0.2

        repaired, report = grade(plan)

        self.assertIn("DOOR_WIDTH_IMPLAUSIBLE", _codes(report))
        self.assertEqual(len(repaired.openings), 1)


class RoomTests(SimpleTestCase):
    def test_a_tiny_room_is_removed(self):
        plan = _plan()
        plan.rooms.append(
            Room(id="R002", name="Sliver", polygon=[(0.0, 0.0), (0.3, 0.0), (0.3, 0.3)])
        )

        repaired, report = grade(plan)

        self.assertIn("ROOM_DEGENERATE", _codes(report))
        self.assertEqual([r.id for r in repaired.rooms], ["R001"])

    def test_a_self_intersecting_room_is_a_hard_error(self):
        plan = _plan()
        plan.rooms = [
            Room(
                id="R001",
                name="Bowtie",
                polygon=[(0.0, 0.0), (4.0, 4.0), (4.0, 0.0), (0.0, 4.0)],
            )
        ]

        _, report = grade(plan)

        self.assertIn("ROOM_SELF_INTERSECTING", _codes(report))
        self.assertFalse(report.is_acceptable())

    def test_overlapping_rooms_are_warned_about(self):
        plan = _plan()
        plan.rooms.append(
            Room(
                id="R002",
                name="Also Living Room",
                polygon=[(1.0, 1.0), (8.0, 1.0), (8.0, 8.0), (1.0, 8.0)],
            )
        )

        _, report = grade(plan)

        self.assertIn("ROOM_OVERLAP", _codes(report))

    def test_duplicate_room_names_are_disambiguated(self):
        plan = _plan(size=20.0)
        plan.rooms = [
            Room(id="R001", name="Bedroom", polygon=[(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]),
            Room(id="R002", name="Bedroom", polygon=[(10.0, 0.0), (14.0, 0.0), (14.0, 4.0), (10.0, 4.0)]),
        ]

        repaired, report = grade(plan)

        self.assertIn("ROOM_DUPLICATE_NAME", _codes(report))
        self.assertEqual([r.name for r in repaired.rooms], ["Bedroom", "Bedroom 2"])


class LevelAndPlanTests(SimpleTestCase):
    def test_a_missing_outline_is_derived_from_the_walls(self):
        plan = _plan()
        plan.levels[0].outline = []

        repaired, report = grade(plan)

        self.assertIn("LEVEL_OUTLINE_MISSING", _codes(report))
        self.assertGreaterEqual(len(repaired.levels[0].outline), 3)
        self.assertAlmostEqual(geom.polygon_area(repaired.levels[0].outline), 100.0, places=1)

    def test_wall_height_is_clamped_under_the_floor_to_floor(self):
        plan = _plan()
        plan.levels[0].floor_to_floor = 2.8
        plan.levels[0].slab_thickness = 0.2
        plan.levels[0].wall_height = 2.7

        repaired, report = grade(plan)

        self.assertIn("LEVEL_HEIGHT_INCONSISTENT", _codes(report))
        self.assertAlmostEqual(repaired.levels[0].wall_height, 2.6)

    def test_an_unknown_scale_is_flagged(self):
        plan = _plan(scale=Scale(source=ScaleSource.UNKNOWN))

        _, report = grade(plan)

        self.assertIn("SCALE_UNKNOWN", _codes(report))

    def test_an_absurd_footprint_is_a_hard_error(self):
        plan = _plan(size=0.8)

        _, report = grade(plan)

        self.assertIn("FOOTPRINT_IMPLAUSIBLE", _codes(report))
        self.assertFalse(report.is_acceptable())

    def test_a_plan_with_no_doors_is_flagged(self):
        plan = _plan()
        plan.openings = []

        _, report = grade(plan)

        self.assertIn("NO_DOORS", _codes(report))

    def test_low_room_coverage_is_flagged(self):
        plan = _plan(size=20.0)
        plan.rooms = [
            Room(id="R001", name="Store", polygon=[(0.0, 0.0), (3.0, 0.0), (3.0, 3.0), (0.0, 3.0)])
        ]

        _, report = grade(plan)

        self.assertIn("ROOM_COVERAGE_LOW", _codes(report))


class ScoringTests(SimpleTestCase):
    def test_one_systematic_mistake_cannot_drive_the_score_to_zero(self):
        """Sixty instances of one code are capped, so the plan stays diagnosable."""
        plan = _plan(size=30.0)
        plan.rooms = []
        plan.openings = []
        # A grid of free-standing, disconnected walls: many findings, one code.
        plan.walls = [
            Wall(
                id=f"W{index + 1:03d}",
                start=(float(index) * 2.0, 0.0),
                end=(float(index) * 2.0 + 1.0, 0.0),
                level_id="L1",
            )
            for index in range(30)
        ]

        _, report = grade(plan)

        self.assertGreater(report.score, 0)
        self.assertLess(report.score, 90)

    def test_an_unrepaired_error_blocks_acceptance_at_any_score(self):
        plan = _plan()
        plan.rooms = [
            Room(
                id="R001",
                name="Bowtie",
                polygon=[(0.0, 0.0), (4.0, 4.0), (4.0, 0.0), (0.0, 4.0)],
            )
        ]

        _, report = grade(plan)

        self.assertTrue(report.unrepaired_errors)
        self.assertFalse(report.is_acceptable(minimum=0))

    def test_the_visual_score_pulls_the_headline_down(self):
        _, report = grade(_plan())
        geometry_only = report.score

        report.visual_score = 40
        self.assertLess(report.score, geometry_only)

    def test_summary_for_model_lists_errors_first(self):
        plan = _plan()
        plan.openings[0].width = 14.0
        plan.scale = Scale(source=ScaleSource.UNKNOWN)

        _, report = grade(plan)
        summary = report.summary_for_model()

        self.assertIn("ERROR", summary)
        self.assertLess(summary.index("ERROR"), summary.index("WARNING"))


class SchemaTests(SimpleTestCase):
    def test_an_opening_hosted_by_a_missing_wall_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            _plan(
                openings=[
                    Opening(
                        id="O001",
                        type=OpeningType.DOOR,
                        wall_id="W404",
                        offset=1.0,
                        width=0.9,
                        height=2.1,
                    )
                ]
            )
        self.assertIn("W404", str(caught.exception))

    def test_duplicate_wall_ids_are_rejected(self):
        walls = _square_walls()
        walls[1].id = "W001"
        with self.assertRaises(ValueError):
            _plan(walls=walls, openings=[])

    def test_a_malformed_element_id_is_rejected(self):
        with self.assertRaises(ValueError):
            Wall(id="wall one", start=(0.0, 0.0), end=(1.0, 0.0))

    def test_a_wall_on_a_missing_level_is_rejected(self):
        walls = _square_walls()
        walls[0].level_id = "L9"
        with self.assertRaises(ValueError) as caught:
            _plan(walls=walls, openings=[])
        self.assertIn("L9", str(caught.exception))


class GeometryTests(SimpleTestCase):
    def test_polygon_area_is_orientation_independent(self):
        square = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
        self.assertAlmostEqual(geom.polygon_area(square), 4.0)
        self.assertAlmostEqual(geom.polygon_area(list(reversed(square))), 4.0)

    def test_point_in_polygon(self):
        square = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
        self.assertTrue(geom.point_in_polygon((2.0, 2.0), square))
        self.assertFalse(geom.point_in_polygon((5.0, 2.0), square))

    def test_shared_corners_do_not_count_as_an_intersection(self):
        self.assertFalse(
            geom.segments_intersect((0.0, 0.0), (1.0, 0.0), (1.0, 0.0), (1.0, 1.0))
        )

    def test_crossing_segments_do_intersect(self):
        self.assertTrue(
            geom.segments_intersect((0.0, 0.0), (2.0, 2.0), (0.0, 2.0), (2.0, 0.0))
        )

    def test_overlap_ratio_of_identical_polygons_is_one(self):
        square = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
        self.assertAlmostEqual(geom.polygons_overlap_ratio(square, square), 1.0, places=2)

    def test_overlap_ratio_of_disjoint_polygons_is_zero(self):
        a = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
        b = [(5.0, 5.0), (7.0, 5.0), (7.0, 7.0), (5.0, 7.0)]
        self.assertEqual(geom.polygons_overlap_ratio(a, b), 0.0)

    def test_convex_hull_of_a_square_keeps_four_corners(self):
        points = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0), (1.0, 1.0)]
        hull = geom.convex_hull(points)
        self.assertEqual(len(hull), 4)
        self.assertAlmostEqual(geom.polygon_area(hull), 4.0)


class VisualFloorTests(SimpleTestCase):
    """A geometrically perfect model of the WRONG building must not pass.

    Guards the rule in `report.MIN_VISUAL_SCORE`: under a plain 70/30 weighting
    a flawless extraction of a different drawing scores exactly the acceptance
    threshold, so the audit could never veto anything.
    """

    def test_a_very_low_visual_score_blocks_a_perfect_geometry_score(self):
        _, report = grade(_plan())
        self.assertEqual(report.geometry_score, 100)
        self.assertTrue(report.is_acceptable())

        report.visual_score = 0

        self.assertEqual(report.score, 70)  # the weighting alone would pass this
        self.assertFalse(report.is_acceptable())

    def test_a_middling_visual_score_still_passes(self):
        _, report = grade(_plan())
        report.visual_score = 75

        self.assertTrue(report.is_acceptable())

    def test_the_floor_is_inclusive(self):
        _, report = grade(_plan())
        report.visual_score = 50
        self.assertFalse(report.is_acceptable())

        report.visual_score = 51
        self.assertTrue(report.is_acceptable())
