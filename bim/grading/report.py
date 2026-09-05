"""The shape of a quality verdict, and how a score is computed from it.

The grader never raises on a bad plan. It returns a report, because "this plan
is 62/100 and here is why" is actionable — by the retry loop, by the UI, and by
the user — while an exception is not. The only thing that raises is a plan whose
internal references do not resolve, and that is caught by the schema itself
before the grader ever sees it.

SCORING
-------
Every plan starts at 100 and loses points per issue. Weights are per-severity
and deliberately coarse; a finer scale would imply a precision this cannot
have. What matters is the ordering: an unbuildable plan must always score below
a merely imperfect one.

Repaired issues cost a fraction of an unrepaired one rather than nothing. A
plan that needed twenty automatic fixes was badly extracted even though it now
builds, and the retry loop should still prefer a cleaner second attempt.

The per-code cap exists because one systematic mistake — say, every wall a few
millimetres short of its neighbour — would otherwise produce 60 issues and a
score of 0, which reads as "hopeless" for a plan that is one snap away from
correct.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Severity(str, Enum):
    # The plan cannot be built, or would build into something structurally
    # wrong. Blocks acceptance unless repaired.
    ERROR = "error"
    # The plan builds, but something is probably not what the drawing showed.
    WARNING = "warning"
    # Worth telling the user; costs nothing.
    INFO = "info"


# Points deducted per issue. Unrepaired first, repaired second.
_WEIGHTS: Dict[Severity, tuple[int, int]] = {
    Severity.ERROR: (14, 4),
    Severity.WARNING: (5, 2),
    Severity.INFO: (1, 0),
}

# No single issue code may cost more than this in total, however many times it
# fires. See the module docstring.
_PER_CODE_CAP = 25

# Below this, the extraction is not worth showing to a user without a retry.
DEFAULT_ACCEPT_SCORE = 70

# A visual score at or below this is disqualifying on its own, whatever the
# geometry score says.
#
# Without this rule the auditor cannot actually veto anything. Geometry is
# weighted at 0.7, so a flawless-but-wrong extraction — one that is internally
# perfect and depicts a different building than the one uploaded — scores
# 100*0.7 + 0*0.3 = 70 and passes the threshold exactly. That is the single
# failure the visual audit exists to catch, and it is the one a weighted average
# is structurally incapable of catching.
#
# 50 is the auditor's own boundary: its rubric calls 25-49 "loosely related to
# the drawing". Set as a hard floor rather than a heavier weight because this is
# a categorical judgement ("this is not that building"), not a matter of degree.
MIN_VISUAL_SCORE = 50


@dataclass
class Issue:
    """One finding. `code` is stable and machine-readable; `message` is not."""

    code: str
    severity: Severity
    message: str
    # Which element this is about, when it is about one. The viewer uses this
    # to let the user jump straight to the offending wall.
    element_id: Optional[str] = None
    element_kind: Optional[str] = None  # "wall" | "opening" | "room" | "level"
    # Set when the grader fixed it. The text says what was done, not what was
    # wrong — the user needs to know the model was changed on their behalf.
    repair: Optional[str] = None
    # Anything numeric worth showing (measured vs expected).
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def repaired(self) -> bool:
        return self.repair is not None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "element_id": self.element_id,
            "element_kind": self.element_kind,
            "repair": self.repair,
            "repaired": self.repaired,
            "detail": self.detail,
        }


@dataclass
class QualityReport:
    """The verdict on one extracted plan."""

    issues: List[Issue] = field(default_factory=list)
    # Populated by the AI auditor when it runs; None when it did not (disabled,
    # errored, or not reached). None is meaningfully different from 0 here.
    visual_score: Optional[int] = None
    visual_notes: List[str] = field(default_factory=list)
    # Counts the grader wants to show regardless of issues: walls, rooms, area.
    stats: Dict[str, Any] = field(default_factory=dict)

    def add(self, issue: Issue) -> None:
        self.issues.append(issue)

    def extend(self, issues: List[Issue]) -> None:
        self.issues.extend(issues)

    # -- derived ----------------------------------------------------------
    @property
    def errors(self) -> List[Issue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def unrepaired_errors(self) -> List[Issue]:
        return [i for i in self.errors if not i.repaired]

    @property
    def warnings(self) -> List[Issue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    @property
    def geometry_score(self) -> int:
        """0-100 from the deterministic checks alone."""
        penalty_by_code: Dict[str, int] = {}
        for issue in self.issues:
            unrepaired_cost, repaired_cost = _WEIGHTS[issue.severity]
            cost = repaired_cost if issue.repaired else unrepaired_cost
            running = penalty_by_code.get(issue.code, 0)
            penalty_by_code[issue.code] = min(running + cost, _PER_CODE_CAP)
        return max(0, 100 - sum(penalty_by_code.values()))

    @property
    def score(self) -> int:
        """The headline number.

        Geometry alone when no visual audit ran, otherwise weighted toward
        geometry: a deterministic check that says "this opening does not fit
        its wall" is a fact, while the auditor's opinion is an opinion. 70/30
        reflects that without letting a confidently wrong extraction — one that
        is internally consistent but bears no resemblance to the drawing — pass
        on geometry alone.
        """
        geometry = self.geometry_score
        if self.visual_score is None:
            return geometry
        return round(geometry * 0.7 + self.visual_score * 0.3)

    @property
    def grade(self) -> str:
        score = self.score
        if score >= 90:
            return "A"
        if score >= 80:
            return "B"
        if score >= 70:
            return "C"
        if score >= 55:
            return "D"
        return "F"

    def is_acceptable(self, minimum: int = DEFAULT_ACCEPT_SCORE) -> bool:
        """Good enough to show the user without another extraction attempt.

        Two things disqualify a plan regardless of its score, because each means
        something a number cannot express:

        - an unrepaired error — part of the plan cannot be built, and a high
          score elsewhere does not change that;
        - a visual score at or below `MIN_VISUAL_SCORE` — the model is
          well-formed but is not this drawing. See that constant for why this
          cannot be expressed as a weighting.
        """
        if self.unrepaired_errors:
            return False
        if self.visual_score is not None and self.visual_score <= MIN_VISUAL_SCORE:
            return False
        return self.score >= minimum

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "grade": self.grade,
            "geometry_score": self.geometry_score,
            "visual_score": self.visual_score,
            "visual_notes": self.visual_notes,
            "acceptable": self.is_acceptable(),
            "counts": {
                "error": len(self.errors),
                "warning": len(self.warnings),
                "info": len([i for i in self.issues if i.severity is Severity.INFO]),
                "repaired": len([i for i in self.issues if i.repaired]),
            },
            "stats": self.stats,
            "issues": [issue.as_dict() for issue in self.issues],
        }

    def summary_for_model(self, limit: int = 25) -> str:
        """The issue list as text, for feeding back into a re-extraction.

        Repaired issues are included but marked, because the model should learn
        not to make them even though this attempt survived them. Truncated,
        because a plan with 200 issues does not need 200 lines to be told it was
        extracted badly.
        """
        ranked = sorted(
            self.issues,
            key=lambda i: (
                {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}[i.severity],
                i.repaired,
            ),
        )
        lines = []
        for issue in ranked[:limit]:
            where = f" [{issue.element_id}]" if issue.element_id else ""
            mark = " (auto-fixed)" if issue.repaired else ""
            lines.append(f"- {issue.severity.value.upper()}{where}: {issue.message}{mark}")
        if len(ranked) > limit:
            lines.append(f"- ...and {len(ranked) - limit} more")
        return "\n".join(lines)
