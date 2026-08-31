from django.conf import settings
from pathlib import Path

from django.db.models import Max
from rest_framework.exceptions import ValidationError

from .models import (
    BOQVersion,
    FloorPlanVersion,
    ProcessingJob,
    ProjectAsset,
    ThreeDVersion,
)


def ensure_step_allowed(project, step_number):
    if not project.allows_step(step_number):
        raise ValidationError(
            {'workflow': f'Step {step_number} is not available in this workflow.'}
        )


def validate_upload(uploaded_file):
    max_bytes = settings.PROJECT_UPLOAD_MAX_BYTES
    if uploaded_file.size > max_bytes:
        max_megabytes = max_bytes // (1024 * 1024)
        raise ValidationError(
            {'file': f'File size cannot exceed {max_megabytes} MB.'}
        )


def validate_image_upload(uploaded_file, field_name='file'):
    validate_upload(uploaded_file)
    allowed_types = {'image/jpeg', 'image/png', 'image/webp'}
    content_type = getattr(uploaded_file, 'content_type', '')
    if content_type not in allowed_types:
        raise ValidationError(
            {field_name: 'Only PNG, JPEG, and WebP images are supported.'}
        )


def validate_floor_plan_upload(uploaded_file):
    validate_upload(uploaded_file)
    allowed_types = {
        'application/pdf',
        'image/jpeg',
        'image/png',
        'image/webp',
    }
    content_type = getattr(uploaded_file, 'content_type', '')
    if content_type not in allowed_types:
        raise ValidationError(
            {'file': 'Only PDF, PNG, JPEG, and WebP floor plans are supported.'}
        )


def create_asset(project, user, uploaded_file, kind):
    validate_upload(uploaded_file)
    return ProjectAsset.objects.create(
        project=project,
        uploaded_by=user,
        kind=kind,
        file=uploaded_file,
        original_name=Path(uploaded_file.name).name,
        content_type=getattr(uploaded_file, 'content_type', ''),
        size=uploaded_file.size,
    )


def effective_floor_plan(project):
    if project.selected_floor_plan and project.selected_floor_plan.image_id:
        return project.selected_floor_plan

    return (
        project.floor_plan_versions.filter(
            status=ProcessingJob.COMPLETED,
            image__isnull=False,
        )
        .order_by('-created_at')
        .first()
    )


def effective_three_d(project):
    if project.selected_three_d and project.selected_three_d.image_id:
        return project.selected_three_d

    return (
        project.three_d_versions.filter(
            status=ProcessingJob.COMPLETED,
            image__isnull=False,
        )
        .order_by('-created_at')
        .first()
    )


def next_boq_version_number(project):
    project.__class__.objects.select_for_update().get(id=project.id)
    maximum = project.boq_versions.aggregate(maximum=Max('version_number'))['maximum']
    return (maximum or 0) + 1


def delete_project_files(project):
    for asset in project.assets.only('file').iterator():
        if asset.file:
            asset.file.delete(save=False)


def enqueue_job(job):
    from .tasks import process_job

    try:
        task_result = process_job.delay(str(job.id))
    except Exception as exc:
        job.status = ProcessingJob.FAILED
        job.error = 'The background job service is unavailable.'
        job.message = 'Could not queue the job.'
        job.save(update_fields=['status', 'error', 'message', 'updated_at'])
        raise ValidationError(
            {'job': 'The background job service is unavailable. Try again shortly.'}
        ) from exc

    job.celery_task_id = task_result.id
    job.save(update_fields=['celery_task_id', 'updated_at'])
    return job


def create_job(project, user, job_type, parameters=None):
    return ProcessingJob.objects.create(
        project=project,
        created_by=user,
        job_type=job_type,
        message='Waiting for a worker.',
        parameters=parameters or {},
    )
