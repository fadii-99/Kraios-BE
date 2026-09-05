"""Floor plan image → graded `BimPlan`.

THE PIPELINE
------------
    prepare image
      → survey pass      (what does this drawing say?)
      → geometry pass    (trace it, anchored to the survey)
          ↳ schema repair loop   (the JSON was broken)
      → normalise        (fill omissions, record them as assumptions)
      → grade            (deterministic geometry checks + repairs)
      → audit            (does it resemble the drawing?)
      → good enough? no → try the geometry pass again with the findings
      → furniture pass   (every desk, chair, wc and basin — once, on the winner)
      → grade again      (the fixtures were not there the first time)

WHY TWO PASSES
--------------
Asked for everything at once, a vision model spends its attention on producing
a large well-formed structure and reads the drawing less carefully — scale gets
guessed, unlabelled rooms get skipped, and a truncated response loses the lot.
Splitting the reading from the tracing means the small, high-value facts
(scale, building type, the room list) are established in a short reliable call,
and the long call is anchored to them. It also means a failed geometry pass can
be retried without paying to re-read the drawing.

The survey is NOT retried. If the drawing was misread, re-reading it with the
same model and the same image produces the same answer; and the grader's
findings are about geometry, so they have nothing to tell the survey.

BEST-OF, NOT LAST-OF
--------------------
The loop keeps the highest-scoring attempt, not the most recent one. A repair
round that is told "your rooms overlap" can fix the rooms and break the walls,
and returning that because it happened to be last would be a regression the
user sees.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from pydantic import ValidationError

from bim.ai import auditor, furnishing, jsonio, prompts
from bim.ai.client import (
    BimModelError,
    BimModelTruncated,
    complete_json,
    vision_messages,
)
from bim.ai.config import bim_ai_settings
from bim.ai.imaging import PreparedImage, prepare
from bim.grading import QualityReport, grade
from bim.normalize import apply_defaults
from bim.schema import BimPlan, BuildingType, defaults_for

logger = logging.getLogger(__name__)


class ExtractionError(RuntimeError):
    """No usable plan could be produced. The message is for logs, not clients."""


@dataclass
class AttemptRecord:
    """One geometry attempt, for the extraction record shown to support."""

    attempt: int
    score: Optional[int] = None
    grade: str = ""
    geometry_score: Optional[int] = None
    visual_score: Optional[int] = None
    schema_repairs: int = 0
    accepted: bool = False
    duration_ms: int = 0
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "attempt": self.attempt,
            "score": self.score,
            "grade": self.grade,
            "geometry_score": self.geometry_score,
            "visual_score": self.visual_score,
            "schema_repairs": self.schema_repairs,
            "accepted": self.accepted,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


@dataclass
class ExtractionResult:
    plan: BimPlan
    report: QualityReport
    survey: Dict[str, Any]
    attempts: List[AttemptRecord] = field(default_factory=list)
    image_width: int = 0
    image_height: int = 0
    source_kind: str = "image"
    total_ms: int = 0

    @property
    def models(self) -> dict:
        return {
            "survey": bim_ai_settings.MODEL_SURVEY,
            "geometry": bim_ai_settings.MODEL_GEOMETRY,
            "furniture": bim_ai_settings.MODEL_FURNITURE,
            "audit": bim_ai_settings.MODEL_AUDIT,
        }

    def as_record(self) -> dict:
        """The audit trail persisted alongside the plan."""
        return {
            "models": self.models,
            "attempts": [a.as_dict() for a in self.attempts],
            "survey": self.survey,
            "image": {
                "width": self.image_width,
                "height": self.image_height,
                "source_kind": self.source_kind,
            },
            "total_ms": self.total_ms,
        }


ProgressCallback = Callable[[int, str], None]


def extract_plan(
    raw: bytes,
    *,
    filename: str = "",
    on_progress: Optional[ProgressCallback] = None,
) -> ExtractionResult:
    """Run the full pipeline. Raises `ExtractionError` if nothing usable came back.

    `ImagePreparationError` (a `ValueError`) propagates untouched — it means the
    upload was bad, which is a different answer to the caller than "the model
    could not read this drawing".

    `on_progress(percent, message)` is called at each stage so a caller can show
    a moving bar over what is otherwise a silent minute. It is advisory: a
    callback that raises would fail an extraction that is going fine, so its
    exceptions are swallowed and logged.
    """
    started = time.monotonic()
    report_progress = _progress_reporter(on_progress)

    image = prepare(raw, filename=filename)
    report_progress(15, "Reading the drawing…")

    survey = _survey(image)
    report_progress(30, "Tracing walls and rooms…")
    building_type = _building_type_from(survey)
    logger.info(
        "bim.extract started type=%s scale_source=%s rooms_surveyed=%d",
        building_type.value,
        (survey.get("scale") or {}).get("source"),
        len(survey.get("rooms") or []),
    )

    base_messages = _geometry_messages(image, survey, building_type)

    best: Optional[ExtractionResult] = None
    attempts: List[AttemptRecord] = []
    last_failure = "no attempt completed"

    for index in range(bim_ai_settings.MAX_GEOMETRY_ATTEMPTS):
        record = AttemptRecord(attempt=index + 1)
        attempt_started = time.monotonic()
        if index:
            report_progress(
                min(85, 40 + index * 20),
                f"Refining the model (pass {index + 1})…",
            )
        messages = list(base_messages)
        if best is not None:
            messages.append(
                {
                    "role": "user",
                    "content": prompts.REPAIR_TEMPLATE.format(
                        score=best.report.score,
                        issues=best.report.summary_for_model(),
                    ),
                }
            )

        try:
            plan = _geometry_attempt(messages, building_type, record)
        except (BimModelError, ExtractionError, ValidationError, ValueError) as exc:
            # ExtractionError here means this attempt's own repair rounds were
            # exhausted, not that the pipeline is over — the outer loop still
            # has attempts left, and a fresh attempt re-reads the drawing rather
            # than continuing to patch one bad answer.
            last_failure = f"{type(exc).__name__}: {exc}"
            record.error = last_failure[:500]
            record.duration_ms = int((time.monotonic() - attempt_started) * 1000)
            attempts.append(record)
            logger.warning("bim.extract attempt %d failed — %s", index + 1, last_failure)
            continue

        repaired, report = grade(plan)
        report_progress(min(90, 55 + index * 20), "Checking the result against the drawing…")
        visual_score, visual_notes = auditor.audit(repaired, image.data_url)
        report.visual_score = visual_score
        report.visual_notes = visual_notes

        record.score = report.score
        record.grade = report.grade
        record.geometry_score = report.geometry_score
        record.visual_score = visual_score
        record.accepted = report.is_acceptable(bim_ai_settings.MIN_ACCEPT_SCORE)
        record.duration_ms = int((time.monotonic() - attempt_started) * 1000)
        attempts.append(record)

        candidate = ExtractionResult(
            plan=repaired,
            report=report,
            survey=survey,
            image_width=image.width,
            image_height=image.height,
            source_kind=image.source_kind,
        )
        if best is None or report.score > best.report.score:
            best = candidate

        logger.info(
            "bim.extract attempt %d/%d score=%d (geometry=%d visual=%s) accepted=%s",
            index + 1,
            bim_ai_settings.MAX_GEOMETRY_ATTEMPTS,
            report.score,
            report.geometry_score,
            visual_score,
            record.accepted,
        )
        if record.accepted:
            break

    if best is None:
        raise ExtractionError(
            f"every geometry attempt failed; last error was {last_failure}"
        )

    # Furniture runs ONCE, on the winner, not inside the attempt loop. It is a
    # separate reading of the same drawing and does not get better or worse with
    # the wall geometry, so paying for it on every attempt would buy nothing.
    # It fails open, so a plan can lose its furniture but never its building.
    report_progress(92, "Reading the furniture…")
    furnished = furnishing.furnish(best.plan, image.data_url)
    if furnished is not best.plan:
        # Re-graded because the fixtures were not there when the report was
        # written, and the stats it carries are shown to the user as facts.
        # The visual audit is NOT re-run: it judges rooms, walls and
        # proportions, none of which furniture changes.
        repaired, report = grade(furnished)
        report.visual_score = best.report.visual_score
        report.visual_notes = best.report.visual_notes
        best.plan = repaired
        best.report = report

    best.attempts = attempts
    best.total_ms = int((time.monotonic() - started) * 1000)
    report_progress(95, "Finishing up…")
    return best


def _progress_reporter(callback: Optional[ProgressCallback]) -> ProgressCallback:
    """Wrap a caller's callback so it can never fail the extraction."""
    if callback is None:
        return lambda percent, message: None

    def report(percent: int, message: str) -> None:
        try:
            callback(percent, message)
        except Exception:  # noqa: BLE001 - progress is advisory, never fatal
            logger.warning("bim.extract progress callback failed", exc_info=True)

    return report


# --------------------------------------------------------------------------
# Pass 1
# --------------------------------------------------------------------------
def _survey(image: PreparedImage) -> Dict[str, Any]:
    """Read the drawing's stated facts. Fails open to an empty survey.

    A missing survey costs accuracy — the geometry pass loses its anchor — but
    it does not stop the pipeline, and a plan extracted without one is still
    graded and still usable. Blocking here would turn one flaky provider call
    into a failed upload.
    """
    try:
        reply = complete_json(
            vision_messages(prompts.SURVEY_SYSTEM, prompts.SURVEY_USER, [image.data_url]),
            model=bim_ai_settings.MODEL_SURVEY,
            max_tokens=bim_ai_settings.MAX_TOKENS_SURVEY,
            operation="survey",
            temperature=0.0,
        )
        return jsonio.parse_object(reply.text)
    except (BimModelError, ValueError) as exc:
        logger.warning("bim.survey failed, continuing without it — %s", exc)
        return {}


def _building_type_from(survey: Dict[str, Any]) -> BuildingType:
    raw = str(survey.get("building_type") or "").strip().lower()
    try:
        return BuildingType(raw)
    except ValueError:
        return BuildingType.OTHER


# --------------------------------------------------------------------------
# Pass 2
# --------------------------------------------------------------------------
def _geometry_messages(
    image: PreparedImage, survey: Dict[str, Any], building_type: BuildingType
) -> List[dict]:
    defaults = defaults_for(building_type)
    survey_block = (
        jsonio.json_dumps(survey)
        if survey
        else "(No survey was available for this drawing — read it from scratch.)"
    )
    return vision_messages(
        prompts.GEOMETRY_SYSTEM,
        prompts.geometry_user_prompt(
            survey_block,
            exterior_hint=defaults.exterior_thickness,
            interior_hint=defaults.interior_thickness,
            sill_hint=defaults.window_sill,
        ),
        [image.data_url],
    )


def _geometry_attempt(
    base_messages: List[dict], building_type: BuildingType, record: AttemptRecord
) -> BimPlan:
    """One geometry call, plus a bounded loop repairing malformed output.

    The two failure kinds are handled differently. Truncation means the answer
    was cut off, so echoing it back only inflates the next request without
    helping — the model is told it ran long and asked to shorten its prose.
    Anything else is shown back to the model verbatim alongside the error, which
    is what lets a weaker model converge.
    """
    messages = list(base_messages)
    last_error: Optional[Exception] = None

    for repair_round in range(bim_ai_settings.MAX_SCHEMA_REPAIRS + 1):
        record.schema_repairs = repair_round
        operation = "geometry" if repair_round == 0 else f"geometry-repair-{repair_round}"
        is_last_round = repair_round == bim_ai_settings.MAX_SCHEMA_REPAIRS

        # `complete_json` is inside the try because truncation is raised by the
        # call itself, and truncation is one of the two things this loop exists
        # to repair. Any other BimModelError is a transport or provider problem
        # that a repair prompt cannot help with, so it propagates to the outer
        # attempt loop untouched.
        try:
            reply = complete_json(
                messages,
                model=bim_ai_settings.MODEL_GEOMETRY,
                max_tokens=bim_ai_settings.MAX_TOKENS_GEOMETRY,
                operation=operation,
                temperature=0.1,
            )
        except BimModelTruncated as exc:
            last_error = exc
            logger.warning("bim.%s was truncated — %s", operation, exc)
            if is_last_round:
                break
            # No echo: the output was cut off, so showing it back costs tokens
            # and teaches nothing. The model is told it ran long instead.
            messages = list(base_messages)
            messages.append(
                {
                    "role": "user",
                    "content": prompts.SCHEMA_REPAIR_TEMPLATE.format(error=str(exc)),
                }
            )
            continue

        try:
            raw = jsonio.parse_object(reply.text)
            return BimPlan.model_validate(apply_defaults(raw, building_type=building_type))
        except (ValidationError, ValueError) as exc:
            last_error = exc
            logger.warning(
                "bim.%s produced unusable output — %s: %s",
                operation,
                type(exc).__name__,
                str(exc)[:400],
            )
            if is_last_round:
                break
            messages = list(base_messages)
            messages.append({"role": "assistant", "content": reply.text[:8000]})
            messages.append(
                {
                    "role": "user",
                    "content": prompts.SCHEMA_REPAIR_TEMPLATE.format(
                        error=str(exc)[:2000]
                    ),
                }
            )

    raise ExtractionError(f"the geometry pass never returned a valid plan: {last_error}")
