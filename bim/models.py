"""Storage for the BIM engine.

TWO TABLES, NO CROSS-APP FOREIGN KEYS
-------------------------------------
`BimSource` is a floor plan somebody uploaded. `BimExtraction` is one attempt at
turning it into a `BimPlan`. Neither references `projects`, `accounts` beyond
the user model, or anything else in this codebase, so dropping these two tables
removes the feature completely and leaves every other app's data intact.

Keeping extractions as rows rather than overwriting one field per source is
deliberate. Re-running an extraction is cheap and users will do it — after a
poor result, after a prompt change, after a model upgrade. Each run is
comparable against the last only if the last one still exists, and a support
question about "why did my model come out wrong" is unanswerable without the
attempt record that produced it.

THE PLAN JSON IS THE PRODUCT
----------------------------
`BimExtraction.plan` holds a serialised `BimPlan` and is the source of truth
for everything downstream — the 3D geometry, the viewer, exports, and later the
user's edits. It is stored as JSON rather than as relational rows on purpose:
the schema is versioned, it is consumed whole, and normalising it into tables
would mean a migration every time a field is added to a wall.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from django.conf import settings
from django.db import models


def bim_source_upload_path(instance, filename):
    return f'bim/{instance.id}/{uuid.uuid4()}-{Path(filename).name}'


class BimSource(models.Model):
    """One uploaded 2D floor plan, and what preparing it discovered."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='bim_sources',
    )
    name = models.CharField(max_length=150)
    file = models.FileField(upload_to=bim_source_upload_path)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=150, blank=True)
    size = models.PositiveBigIntegerField(default=0)

    # Filled in once the image has been prepared, so the UI can show the plan
    # at the right aspect ratio before any extraction has run.
    image_width = models.PositiveIntegerField(default=0)
    image_height = models.PositiveIntegerField(default=0)
    source_kind = models.CharField(max_length=10, blank=True)  # "image" | "pdf"

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['owner', '-created_at'])]

    def __str__(self):
        return self.name


class BimExtraction(models.Model):
    """One run of the extraction pipeline over one source."""

    QUEUED = 'QUEUED'
    PROCESSING = 'PROCESSING'
    COMPLETED = 'COMPLETED'
    FAILED = 'FAILED'

    STATUS_CHOICES = [
        (QUEUED, 'Queued'),
        (PROCESSING, 'Processing'),
        (COMPLETED, 'Completed'),
        (FAILED, 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(
        BimSource,
        on_delete=models.CASCADE,
        related_name='extractions',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='bim_extractions',
        null=True,
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=QUEUED)
    progress = models.PositiveSmallIntegerField(default=0)
    message = models.CharField(max_length=255, blank=True)
    # Internal detail. Never serialised to a client — see serializers.py, which
    # substitutes a generic sentence.
    error = models.TextField(blank=True)

    # The extracted BimPlan, already graded and repaired. Empty until COMPLETED.
    plan = models.JSONField(default=dict, blank=True)
    # QualityReport.as_dict(): score, grade, issues, stats.
    quality = models.JSONField(default=dict, blank=True)
    # The audit trail: which models ran, every attempt and its score, the survey.
    record = models.JSONField(default=dict, blank=True)

    # Denormalised out of `quality` so the list endpoint can order and filter on
    # them without loading and parsing every report.
    score = models.PositiveSmallIntegerField(null=True, blank=True)
    grade = models.CharField(max_length=1, blank=True)

    celery_task_id = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['source', '-created_at']),
            models.Index(fields=['status', 'created_at']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(progress__lte=100),
                name='bim_extraction_progress_lte_100',
            ),
            # One live run per source, enforced in the database rather than by
            # a check-then-insert in the view. Two clients posting at once —
            # or one client double-clicking — would otherwise both see "no run
            # in progress" and start a second extraction, which costs a second
            # set of model calls and produces two results racing to be saved.
            models.UniqueConstraint(
                fields=['source'],
                condition=models.Q(status__in=['QUEUED', 'PROCESSING']),
                name='one_active_bim_extraction_per_source',
            ),
        ]

    def __str__(self):
        return f'{self.source_id} · {self.status} · {self.grade or "-"}'

    @property
    def is_finished(self) -> bool:
        return self.status in (self.COMPLETED, self.FAILED)
