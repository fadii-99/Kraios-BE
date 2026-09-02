import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower


def project_asset_upload_path(instance, filename):
    safe_name = Path(filename).name
    return f'projects/{instance.project_id}/{instance.kind}/{uuid.uuid4()}-{safe_name}'


class Project(models.Model):
    COMPLETE = 'COMPLETE'
    STEP_1_ONLY = 'STEP_1_ONLY'
    STEP_2_ONLY = 'STEP_2_ONLY'
    STEP_3_ONLY = 'STEP_3_ONLY'

    WORKFLOW_CHOICES = [
        (COMPLETE, 'Complete workflow'),
        (STEP_1_ONLY, 'Step 1 only'),
        (STEP_2_ONLY, 'Step 2 only'),
        (STEP_3_ONLY, 'Step 3 only'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='projects',
    )
    name = models.CharField(max_length=150)
    workflow = models.CharField(
        max_length=20,
        choices=WORKFLOW_CHOICES,
        default=COMPLETE,
    )
    selected_floor_plan = models.ForeignKey(
        'FloorPlanVersion',
        on_delete=models.SET_NULL,
        related_name='+',
        null=True,
        blank=True,
    )
    selected_three_d = models.ForeignKey(
        'ThreeDVersion',
        on_delete=models.SET_NULL,
        related_name='+',
        null=True,
        blank=True,
    )
    selected_boq = models.ForeignKey(
        'BOQVersion',
        on_delete=models.SET_NULL,
        related_name='+',
        null=True,
        blank=True,
    )
    step_1_completed_at = models.DateTimeField(null=True, blank=True)
    step_2_completed_at = models.DateTimeField(null=True, blank=True)
    step_3_completed_at = models.DateTimeField(null=True, blank=True)
    boq_skipped_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        constraints = [
            models.UniqueConstraint(
                Lower('name'),
                'owner',
                name='unique_project_name_per_owner',
            ),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.pk:
            original_workflow = (
                Project.objects.filter(pk=self.pk)
                .values_list('workflow', flat=True)
                .first()
            )
            if original_workflow and original_workflow != self.workflow:
                raise ValidationError({'workflow': 'Workflow cannot be changed.'})
        super().save(*args, **kwargs)

    def allows_step(self, step_number):
        allowed_steps = {
            self.COMPLETE: {1, 2, 3, 4},
            self.STEP_1_ONLY: {1, 4},
            self.STEP_2_ONLY: {2, 4},
            self.STEP_3_ONLY: {3, 4},
        }
        return step_number in allowed_steps[self.workflow]


class ProjectAsset(models.Model):
    FLOOR_PLAN = 'FLOOR_PLAN'
    THREE_D_IMAGE = 'THREE_D_IMAGE'
    THREE_D_SNAPSHOT = 'THREE_D_SNAPSHOT'
    MASK = 'MASK'
    BOQ_FILE = 'BOQ_FILE'
    DOCUMENT = 'DOCUMENT'
    UPLOAD = 'UPLOAD'
    ARCHIVE = 'ARCHIVE'

    KIND_CHOICES = [
        (FLOOR_PLAN, '2D floor plan'),
        (THREE_D_IMAGE, '3D image'),
        (THREE_D_SNAPSHOT, 'Frontend WebGL snapshot'),
        (MASK, '3D edit mask'),
        (BOQ_FILE, 'BOQ file'),
        (DOCUMENT, 'Project document'),
        (UPLOAD, 'Uploaded file'),
        (ARCHIVE, 'Project archive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='assets',
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='project_assets',
        null=True,
    )
    kind = models.CharField(max_length=30, choices=KIND_CHOICES)
    file = models.FileField(upload_to=project_asset_upload_path)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=150, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.original_name


class ProcessingJob(models.Model):
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

    FLOOR_PLAN_GENERATE = 'FLOOR_PLAN_GENERATE'
    FLOOR_PLAN_EDIT = 'FLOOR_PLAN_EDIT'
    FLOOR_PLAN_ANALYZE = 'FLOOR_PLAN_ANALYZE'
    THREE_D_GENERATE = 'THREE_D_GENERATE'
    THREE_D_EDIT = 'THREE_D_EDIT'
    THREE_D_ANGLE = 'THREE_D_ANGLE'
    BOQ_GENERATE = 'BOQ_GENERATE'
    PROJECT_ARCHIVE = 'PROJECT_ARCHIVE'

    JOB_TYPE_CHOICES = [
        (FLOOR_PLAN_GENERATE, 'Generate 2D floor plan'),
        (FLOOR_PLAN_EDIT, 'Edit 2D floor plan'),
        (FLOOR_PLAN_ANALYZE, 'Extract floor-plan geometry'),
        (THREE_D_GENERATE, 'Generate 3D image'),
        (THREE_D_EDIT, 'Edit 3D image'),
        (THREE_D_ANGLE, 'Generate 3D viewing angle'),
        (BOQ_GENERATE, 'Generate BOQ'),
        (PROJECT_ARCHIVE, 'Build project archive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='jobs',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='processing_jobs',
        null=True,
    )
    job_type = models.CharField(max_length=40, choices=JOB_TYPE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=QUEUED)
    progress = models.PositiveSmallIntegerField(default=0)
    message = models.CharField(max_length=255, blank=True)
    error = models.TextField(blank=True)
    parameters = models.JSONField(default=dict, blank=True)
    celery_task_id = models.CharField(max_length=255, blank=True)
    output_asset = models.ForeignKey(
        ProjectAsset,
        on_delete=models.SET_NULL,
        related_name='completed_jobs',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['project', 'status', 'created_at']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(progress__lte=100),
                name='processing_job_progress_lte_100',
            ),
        ]

    def __str__(self):
        return f'{self.job_type} - {self.status}'


class FloorPlanVersion(models.Model):
    UPLOADED = 'UPLOADED'
    GENERATED = 'GENERATED'
    EDITED = 'EDITED'

    SOURCE_CHOICES = [
        (UPLOADED, 'Uploaded'),
        (GENERATED, 'Generated'),
        (EDITED, 'Edited'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='floor_plan_versions',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='floor_plan_versions',
        null=True,
    )
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    prompt = models.TextField(blank=True)
    instruction = models.TextField(blank=True)
    prompt_message = models.OneToOneField(
        'ConversationMessage',
        on_delete=models.SET_NULL,
        related_name='floor_plan_version',
        null=True,
        blank=True,
    )
    parent = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        related_name='revisions',
        null=True,
        blank=True,
    )
    image = models.ForeignKey(
        ProjectAsset,
        on_delete=models.SET_NULL,
        related_name='floor_plan_versions',
        null=True,
        blank=True,
    )
    mask = models.ForeignKey(
        ProjectAsset,
        on_delete=models.SET_NULL,
        related_name='masked_floor_plan_edits',
        null=True,
        blank=True,
    )
    job = models.OneToOneField(
        ProcessingJob,
        on_delete=models.SET_NULL,
        related_name='floor_plan_version',
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=20,
        choices=ProcessingJob.STATUS_CHOICES,
        default=ProcessingJob.QUEUED,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class Conversation(models.Model):
    FLOOR_PLAN = 'FLOOR_PLAN'
    THREE_D = 'THREE_D'
    BOQ = 'BOQ'

    KIND_CHOICES = [
        (FLOOR_PLAN, '2D floor-plan generation'),
        (THREE_D, '3D generation'),
        (BOQ, 'BOQ generation'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='conversations',
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['project', 'kind'],
                name='one_conversation_per_project_kind',
            ),
        ]


class ConversationMessage(models.Model):
    USER = 'USER'
    ASSISTANT = 'ASSISTANT'
    SYSTEM = 'SYSTEM'

    ROLE_CHOICES = [
        (USER, 'User'),
        (ASSISTANT, 'Assistant'),
        (SYSTEM, 'System'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='messages',
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    content = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    attachments = models.ManyToManyField(ProjectAsset, related_name='messages', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']


class ThreeDVersion(models.Model):
    GENERATED = 'GENERATED'
    EDITED = 'EDITED'
    ANGLE = 'ANGLE'

    SKETCHUP = 'SKETCHUP'
    PHOTOREALISTIC = 'PHOTOREALISTIC'

    RENDER_STYLE_CHOICES = [
        (SKETCHUP, 'SketchUp'),
        (PHOTOREALISTIC, 'Photorealistic'),
    ]

    ORIGINAL = 'ORIGINAL'
    ISOMETRIC_45 = 'ISOMETRIC_45'

    ANGLE_CHOICES = [
        (ORIGINAL, 'Original'),
        (ISOMETRIC_45, 'Isometric 45°'),
    ]

    SOURCE_CHOICES = [
        (GENERATED, 'Generated'),
        (EDITED, 'Edited'),
        (ANGLE, 'Viewing angle'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='three_d_versions',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='three_d_versions',
        null=True,
    )
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    render_style = models.CharField(
        max_length=30,
        choices=RENDER_STYLE_CHOICES,
        default=SKETCHUP,
    )
    angle = models.CharField(
        max_length=30,
        choices=ANGLE_CHOICES,
        default=ORIGINAL,
    )
    prompt_message = models.OneToOneField(
        ConversationMessage,
        on_delete=models.SET_NULL,
        related_name='three_d_version',
        null=True,
        blank=True,
    )
    # SET_NULL, not PROTECT: the only thing that deletes a FloorPlanVersion is
    # its project cascading, and PROTECT here made deleting any project that had
    # reached Step 2 impossible.
    floor_plan = models.ForeignKey(
        FloorPlanVersion,
        on_delete=models.SET_NULL,
        related_name='three_d_versions',
        null=True,
        blank=True,
    )
    parent = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        related_name='revisions',
        null=True,
        blank=True,
    )
    mask = models.ForeignKey(
        ProjectAsset,
        on_delete=models.SET_NULL,
        related_name='masked_three_d_edits',
        null=True,
        blank=True,
    )
    instruction = models.TextField(blank=True)
    image = models.ForeignKey(
        ProjectAsset,
        on_delete=models.SET_NULL,
        related_name='three_d_versions',
        null=True,
        blank=True,
    )
    job = models.OneToOneField(
        ProcessingJob,
        on_delete=models.SET_NULL,
        related_name='three_d_version',
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=20,
        choices=ProcessingJob.STATUS_CHOICES,
        default=ProcessingJob.QUEUED,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class BOQVersion(models.Model):
    GENERATED = 'GENERATED'
    MANUAL = 'MANUAL'

    SOURCE_CHOICES = [
        (GENERATED, 'AI generated'),
        (MANUAL, 'Manual edit'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='boq_versions',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='boq_versions',
        null=True,
    )
    version_number = models.PositiveIntegerField()
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    source_message = models.ForeignKey(
        ConversationMessage,
        on_delete=models.SET_NULL,
        related_name='boq_versions',
        null=True,
        blank=True,
    )
    parent = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        related_name='revisions',
        null=True,
        blank=True,
    )
    structured_data = models.JSONField(default=dict)
    job = models.OneToOneField(
        ProcessingJob,
        on_delete=models.SET_NULL,
        related_name='boq_version',
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=20,
        choices=ProcessingJob.STATUS_CHOICES,
        default=ProcessingJob.QUEUED,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-version_number']
        constraints = [
            models.UniqueConstraint(
                fields=['project', 'version_number'],
                name='unique_boq_version_per_project',
            ),
        ]


class ProjectDocument(models.Model):
    GENERAL = 'GENERAL'
    PROJECT_BRIEF = 'PROJECT_BRIEF'
    STRUCTURAL_DRAWING = 'STRUCTURAL_DRAWING'
    ESTIMATION = 'ESTIMATION'
    MATERIAL_SPECIFICATION = 'MATERIAL_SPECIFICATION'
    THREE_D_MODEL = 'THREE_D_MODEL'
    MEP_DRAWING = 'MEP_DRAWING'
    HVAC_DRAWING = 'HVAC_DRAWING'
    DOOR_WINDOW_SCHEDULE = 'DOOR_WINDOW_SCHEDULE'
    OTHER = 'OTHER'

    # Display labels are matched verbatim by the BOQ agent prompt's "Type:"
    # detection (app/ai/boq/main_agent.py, section 0.1) — do not reword them.
    DOCUMENT_TYPE_CHOICES = [
        (GENERAL, 'General document'),
        (PROJECT_BRIEF, 'Project brief'),
        (STRUCTURAL_DRAWING, 'Structural drawing'),
        (ESTIMATION, 'Estimation'),
        (MATERIAL_SPECIFICATION, 'Material specification'),
        (THREE_D_MODEL, '3D model'),
        (MEP_DRAWING, 'MEP Drawing'),
        (HVAC_DRAWING, 'HVAC Drawing'),
        (DOOR_WINDOW_SCHEDULE, 'Door & Window Schedule'),
        (OTHER, 'Other'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='documents',
    )
    asset = models.OneToOneField(
        ProjectAsset,
        on_delete=models.CASCADE,
        related_name='document',
    )
    title = models.CharField(max_length=255)
    document_type = models.CharField(
        max_length=40,
        choices=DOCUMENT_TYPE_CHOICES,
        default=GENERAL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title
