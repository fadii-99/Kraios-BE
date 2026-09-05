"""The background extraction task.

IDEMPOTENT BY STATUS TRANSITION
-------------------------------
Celery redelivers. A worker killed mid-run, a broker hiccup, or an at-least-once
delivery all produce a second call with the same id, and an extraction that ran
twice would burn a second set of model calls and race itself to save. So the
task claims its work with a filtered UPDATE (`mark_processing`) and exits
quietly when the claim fails — the row is already PROCESSING or already done,
and either way this delivery has nothing to do.

NO RETRIES
----------
`autoretry_for` is deliberately absent. The transport-level retries that make
sense already happen inside the model client; beyond those, a failure here
means the drawing could not be read or the provider is down, and silently
spending another few minutes of model time on it helps nobody. The user sees
the failure and re-runs when they want to.

ERROR TEXT
----------
Everything caught here is written to `BimExtraction.error`, which is internal.
The serializer replaces it with a generic sentence, so provider messages, file
paths and stack detail never reach a client.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.db import connections
from django.utils import timezone

from bim.ai.extractor import ExtractionError, extract_plan
from bim.ai.imaging import ImagePreparationError
from bim.models import BimExtraction
from bim.services import mark_processing, record_failure, record_success

logger = logging.getLogger(__name__)


@shared_task(name='bim.run_extraction', bind=True)
def run_bim_extraction(self, extraction_id: str) -> str:
    """Extract a BimPlan from a source image. Returns a short status string."""
    try:
        extraction = BimExtraction.objects.select_related('source').get(id=extraction_id)
    except BimExtraction.DoesNotExist:
        # The source was deleted between enqueue and pickup. Nothing to do and
        # nothing wrong — a raise here would only fill the error queue.
        logger.info('bim.extraction %s no longer exists; skipping', extraction_id)
        return 'missing'

    if not mark_processing(extraction, message='Reading the floor plan…', progress=10):
        logger.info(
            'bim.extraction %s was already claimed (status=%s); skipping this delivery',
            extraction_id,
            extraction.status,
        )
        return 'already-claimed'

    try:
        with extraction.source.file.open('rb') as handle:
            raw = handle.read()
    except (OSError, ValueError) as exc:
        logger.exception('bim.extraction %s could not read its source file', extraction_id)
        record_failure(extraction, detail=f'source file unreadable: {exc}')
        return 'failed'

    # Drop the database connection before the model calls. They can hold this
    # thread for minutes, which is long enough for the database or a connection
    # pooler to close an idle connection from its end; the write afterwards
    # would then fail on a socket Django still believes is open. Each call
    # below opens a fresh connection for the few milliseconds it needs.
    connections.close_all()

    def report_progress(percent: int, message: str) -> None:
        BimExtraction.objects.filter(
            id=extraction_id, status=BimExtraction.PROCESSING
        ).update(progress=percent, message=message[:255], updated_at=timezone.now())
        connections.close_all()

    try:
        result = extract_plan(
            raw,
            filename=extraction.source.original_name,
            on_progress=report_progress,
        )
    except ImagePreparationError as exc:
        # Reachable when a file that decoded at upload time no longer does —
        # truncated storage, a swapped file. User-fixable, so it is recorded as
        # a failure rather than raised.
        logger.warning('bim.extraction %s: unreadable image — %s', extraction_id, exc)
        record_failure(extraction, detail=f'image preparation failed: {exc}')
        return 'failed'
    except ExtractionError as exc:
        logger.warning('bim.extraction %s: no usable plan — %s', extraction_id, exc)
        record_failure(extraction, detail=str(exc))
        return 'failed'
    except Exception as exc:  # noqa: BLE001 - the task must never leave a row PROCESSING
        # A row stuck in PROCESSING is invisible to the user as anything other
        # than a spinner that never stops, so every unexpected exception is
        # recorded against the row before it is re-raised for the error queue.
        logger.exception('bim.extraction %s failed unexpectedly', extraction_id)
        record_failure(extraction, detail=f'{type(exc).__name__}: {exc}')
        raise

    record_success(extraction, result)
    logger.info(
        'bim.extraction %s completed grade=%s score=%d attempts=%d in %dms',
        extraction_id,
        result.report.grade,
        result.report.score,
        len(result.attempts),
        result.total_ms,
    )
    return 'completed'
