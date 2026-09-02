import base64
import json
import shutil
import tempfile
import threading
import time
import zipfile

from asgiref.sync import async_to_sync
from celery import shared_task
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.files import File
from django.core.files.base import ContentFile
from django.db import connections, transaction
from django.utils import timezone

from .models import (
    BOQVersion,
    ConversationMessage,
    ProcessingJob,
    ProjectAsset,
)


PLACEHOLDER_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC'
    'AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
)


def notify_job(job):
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f'job_{job.id}',
        {
            'type': 'job.status',
            'data': {
                'id': str(job.id),
                'project': str(job.project_id),
                'job_type': job.job_type,
                'status': job.status,
                'progress': job.progress,
                'message': job.message,
                'parameters': job.parameters,
                'error': (
                    'Processing failed. Please try again or contact support.'
                    if job.status == ProcessingJob.FAILED
                    else ''
                ),
                'output_asset': (
                    str(job.output_asset_id) if job.output_asset_id else None
                ),
                'updated_at': job.updated_at.isoformat(),
            },
        },
    )


def update_job(job, **changes):
    for field, value in changes.items():
        setattr(job, field, value)
    job.save(update_fields=[*changes.keys(), 'updated_at'])
    notify_job(job)


class JobHeartbeat:
    """Keep reporting progress while a long AI call runs.

    A generation used to emit progress 15, then nothing at all until the
    pipeline returned — over a minute of silence for a BOQ turn. The client
    has no way to tell that apart from a dead job, so it showed a failure
    with a retry button for work that was in fact still running and would
    succeed. Idle WebSocket connections also get dropped by proxies in that
    window. So the job reports in on an interval instead.

    Progress creeps toward CEILING and never reaches it: the real 90 and 100
    are published by the task itself once the pipeline is actually done, and
    the bar must never go backwards.

    Runs as a thread beside the pipeline. Every write is a targeted UPDATE
    filtered on the job still being PROCESSING, so it can neither clobber
    the fields the task owns nor resurrect a job that failed or was closed
    out from elsewhere.
    """

    INTERVAL_SECONDS = 10
    FLOOR = 15
    CEILING = 85
    STEP = 5

    def __init__(self, job):
        self._job_id = job.id
        self._progress = max(job.progress, self.FLOOR)
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self._thread = threading.Thread(
            target=self._run, name=f'job-heartbeat-{self._job_id}', daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.INTERVAL_SECONDS)
        return False

    def _run(self):
        try:
            while not self._stop.wait(self.INTERVAL_SECONDS):
                self._tick()
        finally:
            # This thread opened its own connection; the worker will not.
            connections.close_all()

    def _tick(self):
        self._progress = min(self._progress + self.STEP, self.CEILING)
        updated = ProcessingJob.objects.filter(
            id=self._job_id,
            status=ProcessingJob.PROCESSING,
        ).update(
            progress=self._progress,
            message='The AI pipeline is still working on this.',
            updated_at=timezone.now(),
        )
        if not updated:
            self._stop.set()
            return

        job = ProcessingJob.objects.filter(id=self._job_id).first()
        if job is None:
            return
        try:
            notify_job(job)
        except Exception:
            # The saved progress above is what the REST poller reads, so a
            # channel-layer hiccup must not stop the heartbeat.
            pass


def run_placeholder_processing(job):
    delay_seconds = max(0, settings.AI_PLACEHOLDER_DELAY_SECONDS)
    checkpoints = [15, 35, 60, 85]
    sleep_per_checkpoint = delay_seconds / len(checkpoints) if checkpoints else 0

    for progress in checkpoints:
        if sleep_per_checkpoint:
            time.sleep(sleep_per_checkpoint)
        update_job(
            job,
            progress=progress,
            message='Placeholder AI processing is running.',
        )


def create_placeholder_image(job, kind, label):
    filename = f'{label}-{job.id}.png'
    asset = ProjectAsset(
        project=job.project,
        uploaded_by=job.created_by,
        kind=kind,
        original_name=filename,
        content_type='image/png',
        size=len(PLACEHOLDER_PNG),
        metadata={'placeholder': True, 'job_id': str(job.id)},
    )
    asset.file.save(filename, ContentFile(PLACEHOLDER_PNG), save=True)
    return asset


def create_generated_image(job, kind, output):
    asset = ProjectAsset(
        project=job.project,
        uploaded_by=job.created_by,
        kind=kind,
        original_name=output.filename,
        content_type=output.content_type,
        size=len(output.content),
        metadata=output.metadata,
    )
    asset.file.save(
        output.filename,
        ContentFile(output.content),
        save=True,
    )
    return asset


def complete_floor_plan_job(job, pipeline_output=None):
    version = job.floor_plan_version
    if pipeline_output is None:
        asset = create_placeholder_image(
            job,
            ProjectAsset.FLOOR_PLAN,
            'floor-plan',
        )
        assistant_content = 'The placeholder 2D floor-plan generation has completed.'
    else:
        asset = create_generated_image(
            job,
            ProjectAsset.FLOOR_PLAN,
            pipeline_output,
        )
        assistant_content = 'Your 2D floor plan is ready for review.'
    version.image = asset
    version.status = ProcessingJob.COMPLETED
    version.completed_at = timezone.now()
    version.save(update_fields=['image', 'status', 'completed_at'])

    if version.prompt_message_id:
        message = ConversationMessage.objects.create(
            conversation=version.prompt_message.conversation,
            role=ConversationMessage.ASSISTANT,
            content=assistant_content,
        )
        message.attachments.add(asset)
    return asset


def complete_three_d_job(job, pipeline_output=None):
    version = job.three_d_version
    if pipeline_output is None:
        asset = create_placeholder_image(
            job,
            ProjectAsset.THREE_D_IMAGE,
            'three-d',
        )
        assistant_content = 'The placeholder 3D generation has completed.'
    else:
        asset = create_generated_image(
            job,
            ProjectAsset.THREE_D_IMAGE,
            pipeline_output,
        )
        assistant_content = 'Your 3D render is ready for review.'
    version.image = asset
    version.status = ProcessingJob.COMPLETED
    version.completed_at = timezone.now()
    version.save(update_fields=['image', 'status', 'completed_at'])

    if version.prompt_message_id:
        message = ConversationMessage.objects.create(
            conversation=version.prompt_message.conversation,
            role=ConversationMessage.ASSISTANT,
            content=assistant_content,
        )
        message.attachments.add(asset)
    return asset


def complete_floor_plan_analysis_job(job, pipeline_output):
    asset = pipeline_output.asset
    asset.metadata = pipeline_output.metadata
    asset.save(update_fields=['metadata'])
    return asset


def complete_boq_job(job, pipeline_output=None):
    version = job.boq_version
    workflow_pending = False
    if pipeline_output is None:
        version.structured_data = {
            'columns': ['Item', 'Description', 'Quantity', 'Unit', 'Rate', 'Amount'],
            'rows': [
                {
                    'Item': '1',
                    'Description': 'Placeholder construction item',
                    'Quantity': 1,
                    'Unit': 'item',
                    'Rate': 0,
                    'Amount': 0,
                }
            ],
            'placeholder': True,
        }
        assistant_content = f'BOQ version {version.version_number} is ready.'
    else:
        version.structured_data = pipeline_output.structured_data
        assistant_content = pipeline_output.response_text
        workflow_pending = bool(
            pipeline_output.structured_data.get('workflow_pending')
        )
    version.status = ProcessingJob.COMPLETED
    version.completed_at = timezone.now()
    version.save(update_fields=['structured_data', 'status', 'completed_at'])
    if version.source_message_id:
        ConversationMessage.objects.create(
            conversation=version.source_message.conversation,
            role=ConversationMessage.ASSISTANT,
            content=assistant_content,
        )
    if workflow_pending:
        version.delete()
    return None


def archive_folder(asset):
    folders = {
        ProjectAsset.FLOOR_PLAN: '2D',
        ProjectAsset.THREE_D_IMAGE: '3D',
        ProjectAsset.BOQ_FILE: 'BOQ',
        ProjectAsset.DOCUMENT: 'Documents',
        ProjectAsset.MASK: 'Masks',
        ProjectAsset.UPLOAD: 'Uploads',
    }
    return folders.get(asset.kind, 'Other')


def complete_archive_job(job):
    archive_buffer = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024)
    existing_names = set()

    with zipfile.ZipFile(archive_buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        scope = job.parameters.get('scope', 'ALL')
        assets = job.project.assets.exclude(
            kind__in=[ProjectAsset.ARCHIVE, ProjectAsset.THREE_D_SNAPSHOT]
        )
        if scope == 'FLOOR_PLANS':
            assets = assets.filter(kind=ProjectAsset.FLOOR_PLAN)
        elif scope == 'THREE_D':
            assets = assets.filter(kind=ProjectAsset.THREE_D_IMAGE)
        elif scope == 'BOQ':
            assets = assets.filter(kind=ProjectAsset.BOQ_FILE)
        elif scope == 'DOCUMENTS':
            assets = assets.filter(kind__in=[ProjectAsset.DOCUMENT, ProjectAsset.UPLOAD])
        for asset in assets.iterator():
            if not asset.file:
                continue
            base_name = f'{archive_folder(asset)}/{asset.original_name}'
            archive_name = base_name
            suffix = 1
            while archive_name in existing_names:
                archive_name = f'{base_name}-{suffix}'
                suffix += 1
            existing_names.add(archive_name)
            with asset.file.open('rb') as source_file:
                with archive.open(archive_name, 'w') as archive_file:
                    shutil.copyfileobj(source_file, archive_file, length=1024 * 1024)

        if scope in {'ALL', 'BOQ'}:
            for version in job.project.boq_versions.filter(
                status=ProcessingJob.COMPLETED
            ).iterator():
                archive.writestr(
                    f'BOQ/boq-version-{version.version_number}.json',
                    json.dumps(version.structured_data, indent=2),
                )

    scope = job.parameters.get('scope', 'ALL').lower()
    filename = f'project-{job.project_id}-{scope}-{job.id}.zip'
    archive_size = archive_buffer.tell()
    archive_buffer.seek(0)
    asset = ProjectAsset(
        project=job.project,
        uploaded_by=job.created_by,
        kind=ProjectAsset.ARCHIVE,
        original_name=filename,
        content_type='application/zip',
        size=archive_size,
        metadata={'job_id': str(job.id), 'scope': scope.upper()},
    )
    asset.file.save(filename, File(archive_buffer), save=True)
    archive_buffer.close()
    return asset


@shared_task
def process_job(job_id):
    job = ProcessingJob.objects.select_related('project', 'created_by').get(id=job_id)
    update_job(
        job,
        status=ProcessingJob.PROCESSING,
        progress=5,
        message='A worker has started this job.',
        started_at=timezone.now(),
        error='',
    )

    try:
        pipeline_output = None
        if job.job_type == ProcessingJob.PROJECT_ARCHIVE:
            update_job(
                job,
                progress=35,
                message='Preparing the project archive.',
            )
        elif settings.AI_PIPELINE_ENABLED:
            update_job(
                job,
                progress=15,
                message='Preparing inputs for the AI pipeline.',
            )
            from .ai_pipeline import execute_ai_job

            # A render or BOQ turn runs for a minute or more with nothing to
            # report in between; without a heartbeat the client cannot tell
            # that from a dead job.
            with JobHeartbeat(job):
                pipeline_output = execute_ai_job(job)
            update_job(
                job,
                progress=90,
                message='AI processing finished. Saving the result.',
            )
        else:
            run_placeholder_processing(job)

        with transaction.atomic():
            if job.job_type == ProcessingJob.FLOOR_PLAN_ANALYZE:
                if pipeline_output is None:
                    raise RuntimeError('Floor-plan analysis requires the AI pipeline.')
                output_asset = complete_floor_plan_analysis_job(
                    job,
                    pipeline_output,
                )
            elif job.job_type in {
                ProcessingJob.FLOOR_PLAN_GENERATE,
                ProcessingJob.FLOOR_PLAN_EDIT,
            }:
                output_asset = complete_floor_plan_job(job, pipeline_output)
            elif job.job_type in {
                ProcessingJob.THREE_D_GENERATE,
                ProcessingJob.THREE_D_EDIT,
                ProcessingJob.THREE_D_ANGLE,
            }:
                output_asset = complete_three_d_job(job, pipeline_output)
            elif job.job_type == ProcessingJob.BOQ_GENERATE:
                output_asset = complete_boq_job(job, pipeline_output)
            elif job.job_type == ProcessingJob.PROJECT_ARCHIVE:
                output_asset = complete_archive_job(job)
            else:
                raise ValueError(f'Unsupported job type: {job.job_type}')

            update_job(
                job,
                status=ProcessingJob.COMPLETED,
                progress=100,
                message='Job completed successfully.',
                output_asset=output_asset,
                completed_at=timezone.now(),
            )
    except Exception as exc:
        if hasattr(job, 'floor_plan_version'):
            job.floor_plan_version.status = ProcessingJob.FAILED
            job.floor_plan_version.save(update_fields=['status'])
        if hasattr(job, 'three_d_version'):
            job.three_d_version.status = ProcessingJob.FAILED
            job.three_d_version.save(update_fields=['status'])
        if hasattr(job, 'boq_version'):
            job.boq_version.status = ProcessingJob.FAILED
            job.boq_version.save(update_fields=['status'])

        update_job(
            job,
            status=ProcessingJob.FAILED,
            message='Job failed.',
            error=str(exc),
            completed_at=timezone.now(),
        )
        raise
