"""Plan quality grading — the gate between an AI extraction and a 3D model.

`grade()` is the whole public surface. Everything else in this package is an
implementation detail of it:

    repaired_plan, report = grade(extracted_plan)
    if report.is_acceptable():
        ...

The grader is deliberately usable without Django, without the database and
without any AI provider configured, so it can be unit-tested against handmade
plans and reasoned about on its own.
"""
from bim.grading.checks import grade
from bim.grading.report import DEFAULT_ACCEPT_SCORE, Issue, QualityReport, Severity

__all__ = ["grade", "Issue", "QualityReport", "Severity", "DEFAULT_ACCEPT_SCORE"]
