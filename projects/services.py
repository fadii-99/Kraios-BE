import shutil
from datetime import timedelta
from django.conf import settings
from pathlib import Path

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import (
    ConversationMessage,
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
    """Remove everything a project stored, before its rows are deleted.

    Two places hold a project's bytes and deleting the rows reaches neither:
    the Django-stored file per asset, and the project's directory tree in
    media, which used to be left behind as an empty skeleton every time.
    """
    for asset in project.assets.only('file').iterator():
        if asset.file:
            asset.file.delete(save=False)

    _delete_project_media_directory(project)


def _prune_empty_asset_directories(project_id):
    """Drop the per-kind directories a project no longer keeps files in.

    `FieldFile.delete()` unlinks the file and leaves its directory, so
    deleting the last asset of a kind left `projects/<id>/THREE_D_IMAGE/`
    standing, and deleting every block left the project's folder as a shell
    of empty directories. `os.rmdir` is used deliberately: it refuses a
    non-empty directory, so this can never take a file with it.

    Best effort and local-storage only — a leftover directory must not fail
    a delete that has already succeeded.
    """
    media_root = getattr(settings, 'MEDIA_ROOT', None)
    if not media_root:
        return

    try:
        root = Path(media_root).resolve()
        target = (root / 'projects' / str(project_id)).resolve()
        if target.parent != (root / 'projects').resolve() or not target.is_dir():
            return

        for child in target.iterdir():
            if child.is_dir():
                try:
                    child.rmdir()
                except OSError:
                    pass
        try:
            target.rmdir()
        except OSError:
            pass
    except OSError:
        return


def _delete_project_media_directory(project):
    """Remove `media/projects/<id>/` outright, whatever it still holds.

    Per-asset deletion only unlinks files that a row points at, so the
    directory tree survived every project delete, and anything not tracked by
    a row — a file whose row was already gone, or one written by a job that
    crashed before creating it — survived with it. Removing the directory is
    what makes "delete project" leave nothing on disk.

    Local storage only: on a remote backend there is no such directory and
    the per-asset deletes above are already authoritative. Best effort, since
    the project rows are about to go regardless and a leftover directory must
    not fail the request.
    """
    media_root = getattr(settings, 'MEDIA_ROOT', None)
    if not media_root:
        return False

    try:
        root = Path(media_root).resolve()
        target = (root / 'projects' / str(project.id)).resolve()
        # Never step outside media/projects/ — the id is a UUID field, so this
        # is a guard against a future caller, not against the model.
        if target.parent != (root / 'projects').resolve():
            return False
        if not target.is_dir():
            return False
        shutil.rmtree(target)
        return True
    except OSError:
        return False


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


def fail_stale_jobs(older_than_minutes=0, dry_run=False):
    """Mark abandoned jobs as FAILED and report what was closed.

    A job is only ever moved out of QUEUED/PROCESSING by the worker running
    it, so a worker that dies mid-job — a stopped container, a killed
    `celery` process, a machine reboot — leaves the row stuck in PROCESSING
    forever. The client has no way to know that, so it polls
    `/projects/jobs/{id}/` indefinitely and the UI sits on a spinner with
    nothing behind it.

    Only run this when the workers for these jobs are gone: a job that a
    live worker is still processing looks identical from the database, and
    failing it here would strand the worker's later writes against a row the
    client has already been told failed. `older_than_minutes` is the guard —
    keep it comfortably above the longest real generation.

    Mirrors the failure path in `tasks.process_job` so the client sees the
    same shape it would from any other failure, including the linked version
    row, which the history endpoints read.
    """
    cutoff = timezone.now() - timedelta(minutes=max(older_than_minutes, 0))
    stale = ProcessingJob.objects.filter(
        status__in=[ProcessingJob.QUEUED, ProcessingJob.PROCESSING],
        created_at__lte=cutoff,
    ).order_by('created_at')

    closed = []
    for job in stale:
        closed.append(job)
        if dry_run:
            continue

        with transaction.atomic():
            for attribute in ('floor_plan_version', 'three_d_version', 'boq_version'):
                version = getattr(job, attribute, None)
                if version is not None:
                    version.status = ProcessingJob.FAILED
                    version.save(update_fields=['status'])

            job.status = ProcessingJob.FAILED
            job.message = 'Job stopped before it finished.'
            job.error = 'The worker processing this job is no longer running.'
            job.completed_at = timezone.now()
            job.save(
                update_fields=['status', 'message', 'error', 'completed_at', 'updated_at']
            )

        # Best-effort push so an open client stops polling immediately. The
        # saved status above is what actually ends the poll, so a missing or
        # unreachable channel layer must not fail the cleanup.
        try:
            from .tasks import notify_job

            notify_job(job)
        except Exception:
            pass

    return closed


def _job_snapshot_asset(job):
    """The camera snapshot a 3D generation ran from, if it still exists.

    The snapshot is reachable only through `job.parameters`, a JSON field,
    because `_backend_snapshot` renders it inside the task — after the view
    has already created the prompt message — so unlike the render it is on
    no FK and in no `attachments`. That left every snapshot behind when its
    block was deleted. Must be read before the job row goes.
    """
    if job is None:
        return None
    snapshot_id = (job.parameters or {}).get('snapshot_asset_id')
    if not snapshot_id:
        return None
    return ProjectAsset.objects.filter(
        id=snapshot_id,
        project_id=job.project_id,
        kind=ProjectAsset.THREE_D_SNAPSHOT,
    ).first()


def _asset_is_referenced(asset, ignore_attachments=False):
    """Whether anything other than the block being deleted still needs `asset`.

    A message's `attachments` are frequently a *borrowed* reference to
    another version's own output — e.g. an edit's prompt message attaches
    the parent render it edited, and a floor-plan-conversion message attaches
    the source floor plan's image (see `ThreeDGenerateAPIView`,
    `ThreeDEditAPIView` in views.py). Deleting the borrowing message must
    never delete a file another still-live row depends on, so every owner
    field is checked before a physical delete.

    `ignore_attachments` is for the images the block being deleted actually
    *produced*. A later edit's prompt message attaches the render it was
    edited from, so the block that generated that render could never delete
    it — the image stayed on disk and in the database for good. An attachment
    is a reference to context, not ownership, so it does not keep a deleted
    block's own output alive. Rows that genuinely *display* the asset (a
    version's image/mask, a job's output) still protect it either way.
    """
    if (
        ThreeDVersion.objects.filter(Q(image=asset) | Q(mask=asset)).exists()
        or FloorPlanVersion.objects.filter(Q(image=asset) | Q(mask=asset)).exists()
        or ProcessingJob.objects.filter(output_asset=asset).exists()
        # A client-supplied snapshot can be reused by a second generation
        # (a style switch re-renders from the same camera), and that
        # reference lives in the job's parameters rather than on an FK.
        or ProcessingJob.objects.filter(
            parameters__snapshot_asset_id=str(asset.id)
        ).exists()
    ):
        return True
    if ignore_attachments:
        return False
    return ConversationMessage.objects.filter(attachments=asset).exists()


def _exchange_replies(message):
    """The assistant/system messages that answer `message`.

    Each turn stores the reply as its own `ConversationMessage` — there is no
    FK back to the prompt — so a block delete that only removed the user's
    message left the reply behind. For a BOQ turn that reply *is* the
    compiled table (and for 2D/3D it carries the generated image as an
    attachment), so the UI kept showing tables for a block the user had just
    deleted. Everything after the prompt up to the next prompt is that
    prompt's answer.
    """
    later = (
        message.conversation.messages.filter(created_at__gt=message.created_at)
        .order_by('created_at', 'id')
    )
    replies = []
    for candidate in later:
        if candidate.role == ConversationMessage.USER:
            break
        replies.append(candidate)
    return replies


def _messageless_boq_descendants(version):
    """Manual edits of this BOQ table, which belong to no message of their own.

    Editing a row posts a new MANUAL `BOQVersion` with no `source_message`
    (see `BOQManualVersionAPIView`), so nothing links it to a block and a
    block delete could never reach it — it stayed on screen as an editable
    table with no message above it. A descendant that *does* have its own
    source message belongs to another block and is left alone.

    Collected before the parents are deleted: `parent` is SET_NULL, so the
    lineage is gone the moment the generated version is removed.
    """
    found = []
    frontier = [version]
    while frontier:
        current = frontier.pop()
        for child in current.revisions.filter(source_message__isnull=True):
            found.append(child)
            frontier.append(child)
    return found


def delete_message_block(message):
    """Delete a conversation message and the generation "block" it started.

    The frontend shows a user message and the render/plan it produced as one
    unit, but `ConversationMessage` -> `ThreeDVersion`/`FloorPlanVersion` is a
    reverse one-to-one (`prompt_message`) with `on_delete=SET_NULL`, so a bare
    `message.delete()` only detached the version from its message and left
    the version, its `ProcessingJob`, and its `ProjectAsset` (plus the file in
    storage) orphaned forever. This deletes the whole block instead: the
    version(s) the message produced, their processing jobs, and any asset
    file that nothing else still references — all in one transaction, so a
    failure partway through leaves neither row deleted.

    A BOQ-sourced message uses a plain (non-unique) `source_message` FK
    because more than one BOQVersion can share a source message, so that side
    is collected as a queryset rather than a single reverse accessor.

    A block is the whole exchange, not just the prompt: the assistant's reply
    is a separate message row and a manual BOQ edit is a version with no
    message at all, so both are gathered here too. Without that, deleting a
    BOQ block removed the prompt and one table while the reply's own compiled
    table and any edited copy stayed on screen.
    """
    # Split by ownership: what this block produced always goes, while what
    # its message merely attached for context is only removed when nothing
    # else still needs it.
    owned_assets = []
    borrowed_assets = []

    def take_version_assets(version):
        for asset in (version.image, version.mask):
            if asset is not None:
                owned_assets.append(asset)

    with transaction.atomic():
        # Gathered before anything is deleted: the reply is found by position
        # in the conversation, and a manual BOQ edit is found through a
        # `parent` link that SET_NULL destroys as soon as its parent goes.
        replies = _exchange_replies(message)
        boq_versions = list(message.boq_versions.all())
        boq_edits = [
            edit
            for version in boq_versions
            for edit in _messageless_boq_descendants(version)
        ]

        three_d_version = getattr(message, 'three_d_version', None)
        if three_d_version is not None:
            take_version_assets(three_d_version)
            job = three_d_version.job
            # The camera snapshot this render was built from. Guarded rather
            # than owned: a client can point a second generation at the same
            # snapshot, and that reference is only in the other job's
            # parameters.
            snapshot = _job_snapshot_asset(job)
            if snapshot is not None:
                borrowed_assets.append(snapshot)
            three_d_version.delete()
            if job is not None:
                job.delete()

        floor_plan_version = getattr(message, 'floor_plan_version', None)
        if floor_plan_version is not None:
            take_version_assets(floor_plan_version)
            job = floor_plan_version.job
            floor_plan_version.delete()
            if job is not None:
                job.delete()

        for boq_version in boq_versions + boq_edits:
            job = boq_version.job
            boq_version.delete()
            if job is not None:
                job.delete()

        borrowed_assets.extend(message.attachments.all())
        for reply in replies:
            borrowed_assets.extend(reply.attachments.all())

        for reply in replies:
            reply.delete()
        message.delete()

        owned_ids = {asset.id for asset in owned_assets}
        seen_ids = set()
        project_id = None
        for asset in owned_assets + borrowed_assets:
            if asset.id in seen_ids:
                continue
            seen_ids.add(asset.id)
            if _asset_is_referenced(asset, ignore_attachments=asset.id in owned_ids):
                continue
            # Deleting the row also clears the attachment links other
            # messages hold to it, so no dangling M2M rows are left behind.
            project_id = asset.project_id
            asset.file.delete(save=False)
            asset.delete()

        if project_id is not None:
            _prune_empty_asset_directories(project_id)

    # Outside the transaction: the agent's transcript is a file store, so it
    # cannot roll back with the rows, and clearing it is only correct once the
    # delete has actually committed.
    if boq_versions or boq_edits:
        _forget_boq_agent_session(message.conversation.project_id)


def _forget_boq_agent_session(project_id):
    """Drop the BOQ agent's own memory of a project's conversation.

    The agent keeps its transcript in a strands session keyed by project,
    entirely separate from the `ConversationMessage` rows the UI reads. Deleting
    a block cleared the screen and the database while the agent carried on
    quoting the turn that was removed, so the two had to be cleared together.

    Best effort: the rows are already gone, and a stale session must not turn a
    completed delete into a failed request.
    """
    try:
        from app.ai.boq import clear_boq_session

        clear_boq_session(f'project_{project_id}')
    except Exception:
        pass
