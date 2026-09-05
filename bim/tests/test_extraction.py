"""Extraction pipeline tests, with the model provider replaced by a script.

`complete_json` is patched in both places it is imported — `bim.ai.extractor`
and `bim.ai.auditor` — and driven by a queue of canned replies keyed on the
operation name. That keeps the tests about the pipeline's control flow (retry,
repair, best-of, fail-open) rather than about any model's behaviour.
"""
from __future__ import annotations

import copy
import io
import json
from unittest.mock import patch

from django.test import SimpleTestCase
from PIL import Image

from bim.ai import imaging, jsonio, prompts
from bim.ai.client import BimModelError, BimModelTruncated, ModelReply
from bim.ai.extractor import ExtractionError, extract_plan
from bim.normalize import apply_defaults
from bim.schema import BimPlan, BuildingType, defaults_for


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
def _png(width=800, height=600, mode='RGB', colour=(255, 255, 255)) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, (width, height), colour).save(buffer, format='PNG')
    return buffer.getvalue()


_SURVEY = {
    "building_type": "house",
    "drawing_quality": "clear",
    "scale": {"source": "dimension_string", "evidence": "8.00 m", "overall_width_m": 8.0},
    "rooms": [{"name": "Living Room"}, {"name": "Bedroom"}],
}


def _good_plan_json() -> str:
    return json.dumps(prompts.EXAMPLE_PLAN)


def _broken_plan_json() -> str:
    """Valid JSON, invalid plan: the door is hosted by a wall that is not there."""
    plan = copy.deepcopy(prompts.EXAMPLE_PLAN)
    plan["openings"][0]["wall_id"] = "W404"
    return json.dumps(plan)


def _poor_plan_json() -> str:
    """Parses and builds, but carries an UNREPAIRED error.

    A bow-tie room polygon is the cleanest way to make a plan unacceptable no
    matter what it scores: the grader will not guess how to untangle it, so it
    stays as an unrepaired error and `is_acceptable()` is False by rule rather
    than by arithmetic. Defects the grader silently repairs would leave the
    plan acceptable and the retry would never fire.
    """
    plan = copy.deepcopy(prompts.EXAMPLE_PLAN)
    plan["rooms"][0]["polygon"] = [[0.1, 0.1], [4.9, 4.9], [4.9, 0.1], [0.1, 4.9]]
    return json.dumps(plan)


def _worse_plan_json() -> str:
    """The same unrepaired error, plus every soft defect the grader knows."""
    plan = json.loads(_poor_plan_json())
    plan["openings"] = []
    plan["rooms"] = plan["rooms"][:1]
    plan["scale"] = {"source": "unknown", "evidence": "", "confidence": "assumed"}
    return json.dumps(plan)


class ScriptedProvider:
    """Answers `complete_json` from a per-operation queue of replies.

    A queued entry may be a string (returned as the reply text) or an exception
    (raised). When an operation's queue runs dry the last entry repeats, so a
    test only has to script the calls it cares about.
    """

    def __init__(self, **queues):
        self.queues = {key: list(value) for key, value in queues.items()}
        self.calls: list[str] = []

    def __call__(self, messages, *, model, max_tokens, operation, temperature=0.1):
        self.calls.append(operation)
        # "geometry-repair-1" is served by the "geometry" queue.
        key = operation.split("-")[0]
        queue = self.queues.get(key)
        if not queue:
            raise AssertionError(f"no scripted reply for operation {operation!r}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return ModelReply(text=item, model=model)

    def count(self, prefix: str) -> int:
        return len([c for c in self.calls if c.startswith(prefix)])


def _run(provider, *, audit_reply=None, furniture_reply=None):
    """Run the pipeline against a scripted provider.

    ALL THREE call sites are patched, including furniture. `complete_json` is
    imported into each module that uses it, so patching two of them left the
    third making a real HTTP request to the provider — which the pass then
    swallowed, because it fails open. The tests passed and quietly went to the
    network; `furniture_reply=None` now stands in with a refusal instead.
    """
    audit = audit_reply or json.dumps({"score": 95, "verdict": "Matches the drawing."})

    def audit_call(messages, *, model, max_tokens, operation, temperature=0.1):
        if isinstance(audit, Exception):
            raise audit
        return ModelReply(text=audit, model=model)

    def furniture_call(messages, *, model, max_tokens, operation, temperature=0.1):
        if furniture_reply is None:
            raise BimModelError("no furniture reply scripted for this test")
        if isinstance(furniture_reply, Exception):
            raise furniture_reply
        return ModelReply(text=furniture_reply, model=model)

    with patch("bim.ai.extractor.complete_json", provider), patch(
        "bim.ai.auditor.complete_json", audit_call
    ), patch("bim.ai.furnishing.complete_json", furniture_call), patch(
        "bim.ai.config.bim_ai_settings.API_KEY", "test-key"
    ), patch("bim.ai.auditor.bim_ai_settings.API_KEY", "test-key"), patch(
        "bim.ai.furnishing.bim_ai_settings.API_KEY", "test-key"
    ):
        return extract_plan(_png(), filename="plan.png")


# --------------------------------------------------------------------------
# The contract shown to the model must match the contract that is enforced
# --------------------------------------------------------------------------
class PromptContractTests(SimpleTestCase):
    def test_the_worked_example_validates_against_the_schema(self):
        """Guards against the prompt and the schema drifting apart."""
        plan = BimPlan.model_validate(
            apply_defaults(prompts.EXAMPLE_PLAN, building_type=BuildingType.HOUSE)
        )
        self.assertEqual(len(plan.walls), 5)
        self.assertEqual(len(plan.openings), 4)
        self.assertEqual(len(plan.rooms), 2)

    def test_the_worked_example_is_a_clean_plan(self):
        """An example that would not itself pass the grader teaches the wrong thing."""
        from bim.grading import grade

        _, report = grade(
            BimPlan.model_validate(
                apply_defaults(prompts.EXAMPLE_PLAN, building_type=BuildingType.HOUSE)
            )
        )
        self.assertEqual(report.grade, "A")
        self.assertEqual(report.issues, [])

    def test_the_geometry_prompt_renders_with_its_hints(self):
        rendered = prompts.geometry_user_prompt(
            "{}", exterior_hint=0.23, interior_hint=0.15, sill_hint=0.9
        )
        self.assertIn("0.23 m exterior", rendered)
        self.assertIn('"schema_version": "1.0"', rendered)


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------
class ExtractionLoopTests(SimpleTestCase):
    def test_a_clean_first_attempt_stops_after_one_geometry_call(self):
        provider = ScriptedProvider(
            survey=[json.dumps(_SURVEY)], geometry=[_good_plan_json()]
        )

        result = _run(provider)

        self.assertEqual(provider.count("geometry"), 1)
        self.assertEqual(result.report.grade, "A")
        self.assertEqual(len(result.attempts), 1)
        self.assertTrue(result.attempts[0].accepted)
        self.assertEqual(result.survey["building_type"], "house")

    def test_a_failed_survey_does_not_stop_the_extraction(self):
        provider = ScriptedProvider(
            survey=[BimModelError("provider is down")], geometry=[_good_plan_json()]
        )

        result = _run(provider)

        self.assertEqual(result.survey, {})
        self.assertEqual(result.report.grade, "A")

    def test_the_survey_is_never_retried(self):
        provider = ScriptedProvider(
            survey=[json.dumps(_SURVEY)],
            geometry=[_poor_plan_json(), _poor_plan_json(), _good_plan_json()],
        )

        _run(provider)

        self.assertEqual(provider.count("survey"), 1)

    def test_unparseable_output_is_repaired_within_one_attempt(self):
        provider = ScriptedProvider(
            survey=[json.dumps(_SURVEY)],
            geometry=["I'm afraid I can't do that.", _good_plan_json()],
        )

        result = _run(provider)

        self.assertEqual(provider.count("geometry"), 2)
        self.assertEqual(result.attempts[0].schema_repairs, 1)
        self.assertEqual(result.report.grade, "A")

    def test_a_plan_with_a_dangling_reference_is_repaired_not_accepted(self):
        provider = ScriptedProvider(
            survey=[json.dumps(_SURVEY)],
            geometry=[_broken_plan_json(), _good_plan_json()],
        )

        result = _run(provider)

        self.assertEqual(provider.count("geometry"), 2)
        self.assertEqual(result.report.grade, "A")

    def test_truncated_output_is_retried_without_echoing_it_back(self):
        seen: list[list[dict]] = []
        base = ScriptedProvider(
            survey=[json.dumps(_SURVEY)],
            geometry=[BimModelTruncated("ran long"), _good_plan_json()],
        )

        def spy(messages, **kwargs):
            seen.append(messages)
            return base(messages, **kwargs)

        result = _run(spy)

        self.assertEqual(result.report.grade, "A")
        retry_messages = seen[-1]
        self.assertFalse(
            any(message.get("role") == "assistant" for message in retry_messages),
            "a truncated answer must not be echoed back to the model",
        )

    def test_a_poor_result_triggers_another_attempt(self):
        provider = ScriptedProvider(
            survey=[json.dumps(_SURVEY)],
            geometry=[_poor_plan_json(), _good_plan_json()],
        )

        result = _run(provider)

        self.assertEqual(len(result.attempts), 2)
        self.assertFalse(result.attempts[0].accepted)
        self.assertTrue(result.attempts[1].accepted)
        self.assertEqual(result.report.grade, "A")

    def test_the_retry_is_told_what_was_wrong(self):
        seen: list[list[dict]] = []
        base = ScriptedProvider(
            survey=[json.dumps(_SURVEY)],
            geometry=[_poor_plan_json(), _good_plan_json()],
        )

        def spy(messages, **kwargs):
            seen.append(messages)
            return base(messages, **kwargs)

        _run(spy)

        retry_text = seen[-1][-1]["content"]
        self.assertIn("scored", retry_text)
        self.assertIn("ERROR", retry_text)

    def test_the_best_attempt_is_returned_not_the_last(self):
        """A later attempt that scores worse must not replace a better one."""
        provider = ScriptedProvider(
            survey=[json.dumps(_SURVEY)],
            geometry=[_poor_plan_json(), _worse_plan_json(), _worse_plan_json()],
        )

        result = _run(provider)

        self.assertEqual(len(result.attempts), 3)
        scores = [a.score for a in result.attempts if a.score is not None]
        self.assertEqual(result.report.score, max(scores))
        self.assertGreater(scores[0], scores[1], "the fixtures must actually differ")
        # The winning attempt was the first one, which still had its openings.
        self.assertEqual(len(result.plan.openings), 4)

    def test_when_every_attempt_fails_the_pipeline_raises(self):
        provider = ScriptedProvider(
            survey=[json.dumps(_SURVEY)], geometry=["not json at all"]
        )

        with self.assertRaises(ExtractionError):
            _run(provider)

    def test_a_provider_outage_on_geometry_raises(self):
        provider = ScriptedProvider(
            survey=[json.dumps(_SURVEY)], geometry=[BimModelError("502 upstream")]
        )

        with self.assertRaises(ExtractionError):
            _run(provider)

    def test_progress_is_reported_and_a_failing_callback_is_harmless(self):
        provider = ScriptedProvider(
            survey=[json.dumps(_SURVEY)], geometry=[_good_plan_json()]
        )
        seen: list[int] = []

        def flaky(percent, message):
            seen.append(percent)
            raise RuntimeError("the caller's progress sink is broken")

        # Furniture is patched here too, or this test reaches the network.
        with patch("bim.ai.extractor.complete_json", provider), patch(
            "bim.ai.auditor.complete_json",
            lambda *a, **k: ModelReply(text='{"score": 95}', model="m"),
        ), patch(
            "bim.ai.furnishing.complete_json",
            lambda *a, **k: ModelReply(text='{"fixtures": []}', model="m"),
        ), patch("bim.ai.config.bim_ai_settings.API_KEY", "test-key"), patch(
            "bim.ai.auditor.bim_ai_settings.API_KEY", "test-key"
        ), patch("bim.ai.furnishing.bim_ai_settings.API_KEY", "test-key"):
            result = extract_plan(_png(), on_progress=flaky)

        self.assertEqual(result.report.grade, "A")
        self.assertTrue(seen)
        self.assertEqual(seen, sorted(seen), "progress must never go backwards")


class AuditTests(SimpleTestCase):
    def test_a_low_visual_score_pulls_the_headline_down_and_forces_a_retry(self):
        provider = ScriptedProvider(
            survey=[json.dumps(_SURVEY)], geometry=[_good_plan_json()]
        )

        result = _run(
            provider,
            audit_reply=json.dumps(
                {
                    "score": 20,
                    "verdict": "This is not the same building.",
                    "missed_rooms": ["Kitchen", "Bathroom"],
                }
            ),
        )

        self.assertEqual(result.report.geometry_score, 100)
        self.assertEqual(result.report.visual_score, 20)
        self.assertLess(result.report.score, 100)
        self.assertFalse(result.report.is_acceptable())
        self.assertGreater(
            len(result.attempts),
            1,
            "a well-formed model of the wrong building must not be accepted",
        )
        self.assertIn(
            "Kitchen",
            " ".join(result.report.visual_notes),
        )

    def test_an_audit_failure_leaves_the_geometry_score_standing(self):
        provider = ScriptedProvider(
            survey=[json.dumps(_SURVEY)], geometry=[_good_plan_json()]
        )

        result = _run(provider, audit_reply=BimModelError("audit provider down"))

        self.assertIsNone(result.report.visual_score)
        self.assertEqual(result.report.score, 100)

    def test_a_nonsense_audit_score_is_treated_as_no_audit(self):
        provider = ScriptedProvider(
            survey=[json.dumps(_SURVEY)], geometry=[_good_plan_json()]
        )

        result = _run(provider, audit_reply=json.dumps({"verdict": "hmm"}))

        self.assertIsNone(result.report.visual_score)


# --------------------------------------------------------------------------
# Supporting layers
# --------------------------------------------------------------------------
class NormalizeTests(SimpleTestCase):
    def test_missing_level_fields_are_filled_and_recorded(self):
        raw = {
            "levels": [{"id": "L1", "name": "Ground"}],
            "walls": [{"id": "W001", "start": [0, 0], "end": [5, 0]}],
        }

        normalised = apply_defaults(raw, building_type=BuildingType.WAREHOUSE)

        defaults = defaults_for(BuildingType.WAREHOUSE)
        self.assertEqual(normalised["levels"][0]["wall_height"], defaults.wall_height)
        targets = {a["target"] for a in normalised["assumptions"]}
        self.assertIn("L1.wall_height", targets)
        self.assertIn("L1.slab_thickness", targets)

    def test_a_value_the_model_supplied_is_never_overwritten(self):
        raw = {
            "levels": [{"id": "L1", "name": "Ground", "wall_height": 4.2}],
            "walls": [{"id": "W001", "start": [0, 0], "end": [5, 0], "thickness": 0.3}],
        }

        normalised = apply_defaults(raw, building_type=BuildingType.HOUSE)

        self.assertEqual(normalised["levels"][0]["wall_height"], 4.2)
        self.assertEqual(normalised["walls"][0]["thickness"], 0.3)
        self.assertNotIn(
            "L1.wall_height", {a["target"] for a in normalised["assumptions"]}
        )

    def test_wall_thicknesses_are_recorded_as_one_aggregate_assumption(self):
        raw = {
            "levels": [{"id": "L1", "name": "Ground"}],
            "walls": [
                {"id": f"W{i:03d}", "start": [i, 0], "end": [i + 1, 0], "type": "interior"}
                for i in range(1, 21)
            ],
        }

        normalised = apply_defaults(raw, building_type=BuildingType.HOUSE)

        thickness_notes = [
            a for a in normalised["assumptions"] if a["target"] == "walls.thickness"
        ]
        self.assertEqual(len(thickness_notes), 1)
        self.assertIn("20 wall(s)", thickness_notes[0]["reason"])

    def test_a_plan_with_no_level_gets_one(self):
        raw = {"walls": [{"id": "W001", "start": [0, 0], "end": [5, 0]}]}

        normalised = apply_defaults(raw, building_type=BuildingType.SHOP)

        self.assertEqual(normalised["levels"][0]["id"], "L1")
        self.assertEqual(normalised["walls"][0]["level_id"], "L1")

    def test_building_type_defaults_differ_by_type(self):
        self.assertGreater(
            defaults_for(BuildingType.WAREHOUSE).wall_height,
            defaults_for(BuildingType.HOUSE).wall_height,
        )
        self.assertLess(
            defaults_for(BuildingType.SHOP).window_sill,
            defaults_for(BuildingType.HOUSE).window_sill,
        )


class JsonParsingTests(SimpleTestCase):
    def test_a_bare_object_parses(self):
        self.assertEqual(jsonio.parse_object('{"a": 1}'), {"a": 1})

    def test_code_fences_are_stripped(self):
        self.assertEqual(jsonio.parse_object('```json\n{"a": 1}\n```'), {"a": 1})

    def test_a_prose_preamble_is_tolerated(self):
        self.assertEqual(
            jsonio.parse_object('Here is the plan:\n{"a": 1}\nHope that helps.'), {"a": 1}
        )

    def test_a_trailing_comma_is_tolerated(self):
        self.assertEqual(jsonio.parse_object('{"a": 1, "b": [2, 3,],}'), {"a": 1, "b": [2, 3]})

    def test_an_empty_response_raises(self):
        with self.assertRaises(ValueError):
            jsonio.parse_object("   ")

    def test_a_json_array_is_rejected(self):
        with self.assertRaises(ValueError):
            jsonio.parse_object("[1, 2, 3]")

    def test_non_ascii_survives_the_round_trip(self):
        text = jsonio.json_dumps({"room": "بیڈروم"})
        self.assertIn("بیڈروم", text)


class ImagePreparationTests(SimpleTestCase):
    def test_a_plain_png_is_accepted(self):
        prepared = imaging.prepare(_png(800, 600))

        self.assertEqual((prepared.width, prepared.height), (800, 600))
        self.assertEqual(prepared.source_kind, "image")
        self.assertTrue(prepared.data_url.startswith("data:image/png;base64,"))

    def test_transparency_is_flattened_onto_white_not_black(self):
        buffer = io.BytesIO()
        Image.new("RGBA", (40, 40), (0, 0, 0, 0)).save(buffer, format="PNG")

        prepared = imaging.prepare(buffer.getvalue())

        with Image.open(io.BytesIO(prepared.png_bytes)) as result:
            self.assertEqual(result.mode, "RGB")
            self.assertEqual(result.getpixel((10, 10)), (255, 255, 255))

    def test_an_oversized_image_is_downscaled_on_its_longest_edge(self):
        prepared = imaging.prepare(_png(5000, 1000))

        self.assertEqual(max(prepared.width, prepared.height), 2000)
        self.assertEqual(prepared.width / prepared.height, 5.0)

    def test_a_small_image_is_not_upscaled(self):
        prepared = imaging.prepare(_png(300, 200))
        self.assertEqual((prepared.width, prepared.height), (300, 200))

    def test_an_empty_upload_is_rejected(self):
        with self.assertRaises(imaging.ImagePreparationError):
            imaging.prepare(b"")

    def test_a_non_image_is_rejected(self):
        with self.assertRaises(imaging.ImagePreparationError):
            imaging.prepare(b"this is not an image", filename="plan.png")

    def test_a_corrupt_pdf_is_rejected(self):
        with self.assertRaises(imaging.ImagePreparationError):
            imaging.prepare(b"%PDF-1.4 and then nothing useful")

    def test_a_real_pdf_is_rendered(self):
        import fitz

        document = fitz.open()
        page = document.new_page(width=595, height=842)
        page.draw_rect(fitz.Rect(50, 50, 300, 300))
        raw = document.tobytes()
        document.close()

        prepared = imaging.prepare(raw, filename="plan.pdf")

        self.assertEqual(prepared.source_kind, "pdf")
        self.assertEqual(prepared.page_count, 1)
        self.assertGreater(prepared.width, 595)  # rendered above 72 dpi


# --------------------------------------------------------------------------
# The furniture pass
# --------------------------------------------------------------------------
class FurnitureTests(SimpleTestCase):
    """The third pass, in isolation. `furnish` must never raise and never
    return a plan that is worse than the one it was given."""

    def _plan(self):
        return BimPlan.model_validate(
            apply_defaults(prompts.EXAMPLE_PLAN, building_type=BuildingType.HOUSE)
        )

    def _furnish(self, reply):
        from bim.ai import furnishing

        def call(messages, *, model, max_tokens, operation, temperature=0.1):
            if isinstance(reply, Exception):
                raise reply
            return ModelReply(text=reply, model=model)

        with patch("bim.ai.furnishing.complete_json", call), patch(
            "bim.ai.furnishing.bim_ai_settings.API_KEY", "test-key"
        ):
            return furnishing.furnish(self._plan(), "data:image/png;base64,xx")

    def test_fixtures_replace_whatever_the_geometry_pass_left(self):
        result = self._furnish(
            json.dumps({
                "fixtures": [
                    {"category": "desk", "position": [2.0, 2.0], "size": [1.6, 0.8],
                     "height": 0.75, "rotation": 90, "room_id": "R001"},
                    {"category": "chair", "position": [2.0, 3.0], "size": [0.5, 0.5],
                     "room_id": "R001"},
                ]
            })
        )

        self.assertEqual([f.category for f in result.fixtures], ["desk", "chair"])
        self.assertEqual(result.fixtures[0].room_id, "R001")
        # The room decides the level; a fixture cannot be on another storey.
        self.assertEqual(result.fixtures[1].level_id, "L1")

    def test_a_missing_height_is_filled_from_the_category(self):
        result = self._furnish(
            json.dumps({"fixtures": [
                {"category": "wardrobe", "position": [2.0, 2.0], "size": [1.0, 0.6]},
                {"category": "wc", "position": [3.0, 2.0], "size": [0.4, 0.7]},
            ]})
        )

        heights = {f.category: f.height for f in result.fixtures}
        self.assertEqual(heights["wardrobe"], 2.0)
        self.assertEqual(heights["wc"], 0.8)

    def test_a_fixture_outside_the_building_is_dropped(self):
        result = self._furnish(
            json.dumps({"fixtures": [
                {"category": "desk", "position": [2.0, 2.0], "size": [1.2, 0.6]},
                {"category": "desk", "position": [400.0, 400.0], "size": [1.2, 0.6]},
            ]})
        )

        self.assertEqual(len(result.fixtures), 1)

    def test_absurd_sizes_are_dropped(self):
        result = self._furnish(
            json.dumps({"fixtures": [
                {"category": "chair", "position": [2.0, 2.0], "size": [0.01, 0.01]},
                {"category": "table", "position": [2.0, 2.0], "size": [90, 90]},
                {"category": "desk", "position": [2.0, 2.0], "size": [1.2, 0.6]},
            ]})
        )

        self.assertEqual([f.category for f in result.fixtures], ["desk"])

    def test_an_unknown_room_id_becomes_no_room_rather_than_a_bad_reference(self):
        result = self._furnish(
            json.dumps({"fixtures": [
                {"category": "desk", "position": [2.0, 2.0], "size": [1.2, 0.6],
                 "room_id": "R404"},
            ]})
        )

        self.assertEqual(result.fixtures[0].room_id, "")
        self.assertEqual(result.fixtures[0].level_id, "L1")

    def test_ids_are_reissued_so_the_model_cannot_collide_them(self):
        result = self._furnish(
            json.dumps({"fixtures": [
                {"id": "SAME", "category": "desk", "position": [2.0, 2.0], "size": [1.2, 0.6]},
                {"id": "SAME", "category": "chair", "position": [3.0, 2.0], "size": [0.5, 0.5]},
            ]})
        )

        self.assertEqual([f.id for f in result.fixtures], ["F001", "F002"])

    def test_the_fixture_ceiling_is_enforced(self):
        from bim.ai.config import bim_ai_settings

        many = [
            {"category": "chair", "position": [2.0, 2.0], "size": [0.5, 0.5]}
            for _ in range(bim_ai_settings.MAX_FIXTURES + 25)
        ]
        result = self._furnish(json.dumps({"fixtures": many}))

        self.assertEqual(len(result.fixtures), bim_ai_settings.MAX_FIXTURES)

    def test_a_provider_failure_leaves_the_plan_untouched(self):
        original = self._plan()
        result = self._furnish(BimModelError("furniture provider down"))

        self.assertEqual(len(result.fixtures), len(original.fixtures))

    def test_an_unparseable_reply_leaves_the_plan_untouched(self):
        original = self._plan()
        result = self._furnish("sorry, I cannot see the drawing")

        self.assertEqual(len(result.fixtures), len(original.fixtures))

    def test_an_empty_list_leaves_the_plan_untouched(self):
        """An empty answer is more likely a failed reading than an empty room."""
        original = self._plan()
        result = self._furnish(json.dumps({"fixtures": []}))

        self.assertEqual(len(result.fixtures), len(original.fixtures))

    def test_disabling_the_pass_skips_it_entirely(self):
        from bim.ai import furnishing

        with patch("bim.ai.furnishing.bim_ai_settings.FURNITURE_ENABLED", False), patch(
            "bim.ai.furnishing.complete_json"
        ) as call:
            result = furnishing.furnish(self._plan(), "data:image/png;base64,xx")

        call.assert_not_called()
        self.assertEqual(len(result.fixtures), 1)


class FurnitureInThePipelineTests(SimpleTestCase):
    def test_the_furniture_pass_runs_once_on_the_winning_attempt(self):
        provider = ScriptedProvider(
            survey=[json.dumps(_SURVEY)],
            geometry=[_poor_plan_json(), _good_plan_json()],
        )
        furniture = json.dumps({
            "fixtures": [
                {"category": "desk", "position": [2.0, 2.0], "size": [1.6, 0.8],
                 "height": 0.75, "room_id": "R001"},
            ]
        })
        result = _run(provider, furniture_reply=furniture)

        # Two geometry attempts, but only ONE furniture call — the plan carries
        # the single desk once, not once per attempt.
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual([f.category for f in result.plan.fixtures], ["desk"])

    def test_the_report_is_regraded_so_its_fixture_count_is_true(self):
        provider = ScriptedProvider(
            survey=[json.dumps(_SURVEY)], geometry=[_good_plan_json()]
        )
        furniture = json.dumps({
            "fixtures": [
                {"category": "chair", "position": [2.0, 2.0], "size": [0.5, 0.5],
                 "room_id": "R001"},
                {"category": "chair", "position": [3.0, 2.0], "size": [0.5, 0.5],
                 "room_id": "R001"},
            ]
        })

        result = _run(provider, furniture_reply=furniture)

        self.assertEqual(result.report.stats["fixtures"], 2)
        # The visual audit is not re-run — furniture does not change the rooms
        # it judged — so its score survives the second grading.
        self.assertEqual(result.report.visual_score, 95)
