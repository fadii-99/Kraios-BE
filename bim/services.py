"""Domain operations for the BIM engine.

Everything here is callable from a view, a Celery task, a management command
and a test alike, and every multi-row change is wrapped in a transaction. Views
in this app do authentication, parsing and response shaping; the decisions live
here.

UPLOADS ARE VALIDATED BY DECODING THEM
--------------------------------------
`create_source` prepares the image during the request rather than in the
background task. It costs a second, and it means a file that is not really a
drawing — a renamed .zip, a corrupt PDF, a 300 MB scan — is refused with a 400
the user can act on, instead of being accepted and then failing a minute later
as a job with an opaque error. Content type and extension are checked first,
but neither is trusted: the decode is the actual gate.
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from bim.ai.config import bim_ai_settings
from bim.ai.imaging import ImagePreparationError, prepare
from bim.models import BimExtraction, BimSource

logger = logging.getLogger(__name__)

# Checked before decoding as a cheap first filter. The decode is what actually
# decides, so this list can stay generous.
ALLOWED_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff', '.pdf',
}

# How many sources one user may hold. Not a business rule — a guard against an
# automated client filling the disk. Raise it when there is a reason to.
MAX_SOURCES_PER_OWNER = 200


def validate_upload(uploaded_file) -> None:
    """Cheap checks that do not require reading the whole file."""
    if uploaded_file is None:
        raise ValidationError({'file': 'A floor plan file is required.'})

    limit = bim_ai_settings.MAX_UPLOAD_BYTES
    if uploaded_file.size > limit:
        raise ValidationError(
            {'file': f'The file must be {limit // (1024 * 1024)} MB or smaller.'}
        )
    if uploaded_file.size == 0:
        raise ValidationError({'file': 'The file is empty.'})

    name = (uploaded_file.name or '').lower()
    if not any(name.endswith(extension) for extension in sorted(ALLOWED_EXTENSIONS)):
        raise ValidationError(
            {'file': 'Upload a PNG, JPEG, WebP, BMP, TIFF or PDF floor plan.'}
        )


@transaction.atomic
def create_source(*, owner, uploaded_file, name: str = '') -> BimSource:
    """Store an uploaded floor plan after proving it can be read.

    Raises `ValidationError` for anything the user can fix.
    """
    validate_upload(uploaded_file)

    if BimSource.objects.filter(owner=owner).count() >= MAX_SOURCES_PER_OWNER:
        raise ValidationError(
            {'file': 'You have reached the limit for stored floor plans. '
                     'Delete one before uploading another.'}
        )

    uploaded_file.seek(0)
    raw = uploaded_file.read()
    try:
        prepared = prepare(raw, filename=uploaded_file.name or '')
    except ImagePreparationError as exc:
        # The message is written for a user and carries no internal detail, so
        # it is safe to return verbatim.
        raise ValidationError({'file': str(exc)}) from exc

    source = BimSource(
        owner=owner,
        name=(name or uploaded_file.name or 'Floor plan').strip()[:150],
        original_name=(uploaded_file.name or 'floor-plan')[:255],
        content_type=(getattr(uploaded_file, 'content_type', '') or '')[:150],
        size=uploaded_file.size,
        image_width=prepared.width,
        image_height=prepared.height,
        source_kind=prepared.source_kind,
    )
    uploaded_file.seek(0)
    source.file.save(uploaded_file.name or 'floor-plan', uploaded_file, save=False)
    source.save()
    logger.info(
        'bim.source created id=%s owner=%s kind=%s %dx%d',
        source.id, owner.pk, prepared.source_kind, prepared.width, prepared.height,
    )
    return source


def start_extraction(*, source: BimSource, user) -> BimExtraction:
    """Queue an extraction for `source`, refusing a second concurrent run.

    The uniqueness of the live run is enforced by a partial unique index, so
    the IntegrityError below is the real check — a prior `.exists()` would be a
    race, not a guard.
    """
    try:
        with transaction.atomic():
            extraction = BimExtraction.objects.create(
                source=source,
                created_by=user,
                status=BimExtraction.QUEUED,
                message='Queued.',
            )
    except IntegrityError as exc:
        logger.info('bim.extraction rejected as duplicate source=%s', source.id)
        raise ValidationError(
            {'detail': 'An extraction is already running for this floor plan.'}
        ) from exc

    enqueue_extraction(extraction)
    return extraction


def enqueue_extraction(extraction: BimExtraction) -> None:
    """Hand the extraction to Celery, after its row is committed.

    `on_commit` matters: without it the worker can pick the task up and query
    for a row this transaction has not written yet, and the task fails with a
    DoesNotExist for work that is perfectly valid.
    """
    from bim.tasks import run_bim_extraction

    def _dispatch():
        result = run_bim_extraction.delay(str(extraction.id))
        BimExtraction.objects.filter(id=extraction.id).update(
            celery_task_id=str(result.id)
        )

    transaction.on_commit(_dispatch)


def mark_processing(extraction: BimExtraction, *, message: str, progress: int) -> bool:
    """Move a queued extraction to PROCESSING. False if it was not queued.

    Filtered on the current status so a retried Celery delivery cannot restart
    an extraction that already completed — the second delivery updates nothing
    and the task exits.
    """
    updated = BimExtraction.objects.filter(
        id=extraction.id, status=BimExtraction.QUEUED
    ).update(
        status=BimExtraction.PROCESSING,
        message=message[:255],
        progress=progress,
        started_at=timezone.now(),
        updated_at=timezone.now(),
    )
    return bool(updated)


def record_success(extraction: BimExtraction, result) -> None:
    """Persist a completed extraction. `result` is an `ExtractionResult`."""
    report = result.report.as_dict()
    BimExtraction.objects.filter(id=extraction.id).update(
        status=BimExtraction.COMPLETED,
        progress=100,
        message=f'Extracted with grade {result.report.grade} ({result.report.score}/100).',
        error='',
        plan=result.plan.model_dump(mode='json'),
        quality=report,
        record=result.as_record(),
        score=result.report.score,
        grade=result.report.grade,
        completed_at=timezone.now(),
        updated_at=timezone.now(),
    )


def record_failure(extraction: BimExtraction, *, detail: str) -> None:
    """Persist a failed extraction. `detail` is internal and never returned."""
    BimExtraction.objects.filter(id=extraction.id).update(
        status=BimExtraction.FAILED,
        message='Extraction failed.',
        error=detail[:4000],
        completed_at=timezone.now(),
        updated_at=timezone.now(),
    )


def latest_extraction(source: BimSource):
    """The most recent extraction for a source, or None."""
    return source.extractions.order_by('-created_at').first()
