from rest_framework import serializers

from .models import (
    BOQVersion,
    ConversationMessage,
    FloorPlanVersion,
    ProcessingJob,
    Project,
    ProjectAsset,
    ProjectDocument,
    ThreeDVersion,
)


class ProjectAssetSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = ProjectAsset
        fields = (
            'id',
            'kind',
            'original_name',
            'content_type',
            'size',
            'metadata',
            'download_url',
            'created_at',
        )

    def get_download_url(self, asset):
        request = self.context.get('request')
        path = f'/api/v1/projects/{asset.project_id}/assets/{asset.id}/download/'
        return request.build_absolute_uri(path) if request else path


class ProcessingJobSerializer(serializers.ModelSerializer):
    websocket_path = serializers.SerializerMethodField()
    error = serializers.SerializerMethodField()

    class Meta:
        model = ProcessingJob
        fields = (
            'id',
            'project',
            'job_type',
            'status',
            'progress',
            'message',
            'error',
            'parameters',
            'output_asset',
            'websocket_path',
            'created_at',
            'started_at',
            'completed_at',
            'updated_at',
        )

    def get_websocket_path(self, job):
        return f'/ws/jobs/{job.id}/'

    def get_error(self, job):
        if job.status == ProcessingJob.FAILED:
            return 'Processing failed. Please try again or contact support.'
        return ''


class ConversationMessageSerializer(serializers.ModelSerializer):
    attachments = ProjectAssetSerializer(many=True, read_only=True)

    class Meta:
        model = ConversationMessage
        fields = ('id', 'role', 'content', 'metadata', 'attachments', 'created_at')


class FloorPlanVersionSerializer(serializers.ModelSerializer):
    prompt_message = ConversationMessageSerializer(read_only=True)
    image = ProjectAssetSerializer(read_only=True)
    mask = ProjectAssetSerializer(read_only=True)
    job = ProcessingJobSerializer(read_only=True)
    selected = serializers.SerializerMethodField()

    class Meta:
        model = FloorPlanVersion
        fields = (
            'id',
            'source',
            'prompt',
            'instruction',
            'prompt_message',
            'parent',
            'image',
            'mask',
            'job',
            'status',
            'selected',
            'created_at',
            'completed_at',
        )

    def get_selected(self, version):
        return version.project.selected_floor_plan_id == version.id


class ThreeDVersionSerializer(serializers.ModelSerializer):
    prompt_message = ConversationMessageSerializer(read_only=True)
    mask = ProjectAssetSerializer(read_only=True)
    image = ProjectAssetSerializer(read_only=True)
    job = ProcessingJobSerializer(read_only=True)
    selected = serializers.SerializerMethodField()

    class Meta:
        model = ThreeDVersion
        fields = (
            'id',
            'source',
            'render_style',
            'angle',
            'prompt_message',
            'floor_plan',
            'parent',
            'mask',
            'instruction',
            'image',
            'job',
            'status',
            'selected',
            'created_at',
            'completed_at',
        )

    def get_selected(self, version):
        return version.project.selected_three_d_id == version.id


class BOQVersionSerializer(serializers.ModelSerializer):
    job = ProcessingJobSerializer(read_only=True)
    selected = serializers.SerializerMethodField()

    class Meta:
        model = BOQVersion
        fields = (
            'id',
            'version_number',
            'source',
            'source_message',
            'parent',
            'structured_data',
            'job',
            'status',
            'selected',
            'created_at',
            'completed_at',
        )

    def get_selected(self, version):
        return version.project.selected_boq_id == version.id


class ProjectSerializer(serializers.ModelSerializer):
    workflow_display = serializers.CharField(source='get_workflow_display', read_only=True)
    workflow_state = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = (
            'id',
            'name',
            'workflow',
            'workflow_display',
            'selected_floor_plan',
            'selected_three_d',
            'selected_boq',
            'step_1_completed_at',
            'step_2_completed_at',
            'step_3_completed_at',
            'boq_skipped_at',
            'completed_at',
            'workflow_state',
            'created_at',
            'updated_at',
        )
        read_only_fields = (
            'selected_floor_plan',
            'selected_three_d',
            'selected_boq',
            'step_1_completed_at',
            'step_2_completed_at',
            'step_3_completed_at',
            'boq_skipped_at',
            'completed_at',
            'created_at',
            'updated_at',
        )

    def get_workflow_state(self, project):
        step_1_complete = project.selected_floor_plan_id is not None
        step_2_complete = project.selected_three_d_id is not None
        step_3_complete = (
            project.selected_boq_id is not None
            or project.boq_skipped_at is not None
        )

        if project.workflow == Project.COMPLETE:
            if not step_1_complete:
                current_step = 1
            elif not step_2_complete:
                current_step = 2
            elif not step_3_complete:
                current_step = 3
            else:
                current_step = 4
        elif project.workflow == Project.STEP_1_ONLY:
            current_step = 4 if step_1_complete else 1
        elif project.workflow == Project.STEP_2_ONLY:
            current_step = 4 if step_2_complete else 2
        else:
            current_step = 4 if step_3_complete else 3

        return {
            'current_step': current_step,
            'step_1_complete': step_1_complete,
            'step_2_complete': step_2_complete,
            'step_3_complete': step_3_complete,
            'boq_skipped': project.boq_skipped_at is not None,
            'is_finished': project.completed_at is not None,
        }

    def validate(self, attrs):
        if self.instance and 'workflow' in attrs:
            if attrs['workflow'] != self.instance.workflow:
                raise serializers.ValidationError(
                    {'workflow': 'Workflow cannot be changed after project creation.'}
                )
        return attrs

    def validate_name(self, name):
        request = self.context.get('request')
        if request is None or not request.user.is_authenticated:
            return name

        projects = Project.objects.filter(owner=request.user, name__iexact=name)
        if self.instance:
            projects = projects.exclude(id=self.instance.id)
        if projects.exists():
            raise serializers.ValidationError(
                'You already have a project with this name.'
            )
        return name


class PromptSerializer(serializers.Serializer):
    prompt = serializers.CharField(max_length=5000)
    parent_version_id = serializers.UUIDField(required=False)
    retry_message_id = serializers.UUIDField(required=False)


class FloorPlanEditSerializer(serializers.Serializer):
    original_version_id = serializers.UUIDField()
    instruction = serializers.CharField(max_length=5000)
    mask = serializers.FileField()


class FloorPlanAnalyzeSerializer(serializers.Serializer):
    floor_plan_version_id = serializers.UUIDField(required=False)


class ThreeDSnapshotUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    floor_plan_version_id = serializers.UUIDField()
    rooms = serializers.JSONField(required=False, default=list)

    def validate_rooms(self, rooms):
        if not isinstance(rooms, list):
            raise serializers.ValidationError('Rooms must be a JSON array.')
        if len(rooms) > 500:
            raise serializers.ValidationError('At most 500 rooms are supported.')
        for room in rooms:
            if not isinstance(room, (dict, str)):
                raise serializers.ValidationError(
                    'Every room must be an object or a room name.'
                )
        return rooms


class ThreeDGenerateSerializer(serializers.Serializer):
    prompt = serializers.CharField(max_length=5000)
    retry_message_id = serializers.UUIDField(required=False)
    floor_plan_version_id = serializers.UUIDField(required=False)
    snapshot_asset_id = serializers.UUIDField(required=False)
    original_version_id = serializers.UUIDField(required=False)
    style_reference_asset_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        default=list,
        max_length=5,
    )
    render_style = serializers.ChoiceField(
        choices=ThreeDVersion.RENDER_STYLE_CHOICES,
        default=ThreeDVersion.SKETCHUP,
    )


class ThreeDEditSerializer(serializers.Serializer):
    original_version_id = serializers.UUIDField()
    instruction = serializers.CharField(max_length=5000)
    mask = serializers.FileField()


class ThreeDAngleSerializer(serializers.Serializer):
    original_version_id = serializers.UUIDField()
    # ORIGINAL is the camera an existing render already has, so the isometric
    # 45-degree view is the only angle this endpoint can produce.
    angle = serializers.ChoiceField(
        choices=[(ThreeDVersion.ISOMETRIC_45, 'Isometric 45 degrees')],
        default=ThreeDVersion.ISOMETRIC_45,
    )


class BOQGenerateSerializer(serializers.Serializer):
    prompt = serializers.CharField(max_length=5000)
    retry_message_id = serializers.UUIDField(required=False)


class BOQManualVersionSerializer(serializers.Serializer):
    structured_data = serializers.JSONField()
    parent_version_id = serializers.UUIDField(required=False)

    def validate_structured_data(self, data):
        if not isinstance(data, (dict, list)):
            raise serializers.ValidationError('BOQ data must be an object or a list.')
        return data


class ProjectDocumentSerializer(serializers.ModelSerializer):
    asset = ProjectAssetSerializer(read_only=True)
    document_type_display = serializers.CharField(
        source='get_document_type_display',
        read_only=True,
    )

    class Meta:
        model = ProjectDocument
        fields = (
            'id',
            'title',
            'document_type',
            'document_type_display',
            'asset',
            'created_at',
            'updated_at',
        )


class ProjectDocumentUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    title = serializers.CharField(max_length=255, required=False, allow_blank=True)
    document_type = serializers.ChoiceField(
        choices=ProjectDocument.DOCUMENT_TYPE_CHOICES,
        default=ProjectDocument.GENERAL,
    )


class ProjectDocumentUpdateSerializer(serializers.Serializer):
    file = serializers.FileField(required=False)
    title = serializers.CharField(max_length=255, required=False, allow_blank=False)
    document_type = serializers.ChoiceField(
        choices=ProjectDocument.DOCUMENT_TYPE_CHOICES,
        required=False,
    )

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError('At least one field is required.')
        return attrs


class ProjectArchiveRequestSerializer(serializers.Serializer):
    ALL = 'ALL'
    FLOOR_PLANS = 'FLOOR_PLANS'
    THREE_D = 'THREE_D'
    BOQ = 'BOQ'
    DOCUMENTS = 'DOCUMENTS'

    SCOPE_CHOICES = [
        (ALL, 'All deliverables'),
        (FLOOR_PLANS, '2D floor plans'),
        (THREE_D, '3D renders'),
        (BOQ, 'BOQ files'),
        (DOCUMENTS, 'Documents'),
    ]

    scope = serializers.ChoiceField(choices=SCOPE_CHOICES, default=ALL)
