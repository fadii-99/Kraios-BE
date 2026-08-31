import csv
import json

from django.db import IntegrityError, transaction
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    BOQVersion,
    Conversation,
    ConversationMessage,
    FloorPlanVersion,
    ProcessingJob,
    Project,
    ProjectAsset,
    ProjectDocument,
    ThreeDVersion,
)
from .serializers import (
    BOQGenerateSerializer,
    BOQManualVersionSerializer,
    BOQVersionSerializer,
    ConversationMessageSerializer,
    FloorPlanEditSerializer,
    FloorPlanVersionSerializer,
    ProcessingJobSerializer,
    ProjectArchiveRequestSerializer,
    ProjectAssetSerializer,
    ProjectDocumentSerializer,
    ProjectDocumentUpdateSerializer,
    ProjectDocumentUploadSerializer,
    ProjectSerializer,
    PromptSerializer,
    ThreeDAngleSerializer,
    ThreeDEditSerializer,
    ThreeDGenerateSerializer,
    ThreeDVersionSerializer,
)

from .services import (
    create_asset,
    create_job,
    delete_project_files,
    effective_floor_plan,
    effective_three_d,
    enqueue_job,
    ensure_step_allowed,
    next_boq_version_number,
    validate_floor_plan_upload,
    validate_image_upload,
    validate_upload,
)


def owned_project(request, project_id):
    return get_object_or_404(Project, id=project_id, owner=request.user)


def conversation_for(project, kind):
    conversation, _ = Conversation.objects.get_or_create(
        project=project,
        kind=kind,
    )
    return conversation


def persist_floor_plan_selection(project, version):
    selection_changed = project.selected_floor_plan_id != version.id
    project.selected_floor_plan = version
    project.step_1_completed_at = project.step_1_completed_at or timezone.now()
    update_fields = ['selected_floor_plan', 'step_1_completed_at', 'updated_at']
    if selection_changed:
        project.selected_three_d = None
        project.selected_boq = None
        project.step_2_completed_at = None
        project.step_3_completed_at = None
        project.boq_skipped_at = None
        project.completed_at = None
        update_fields.extend(
            [
                'selected_three_d',
                'selected_boq',
                'step_2_completed_at',
                'step_3_completed_at',
                'boq_skipped_at',
                'completed_at',
            ]
        )
    project.save(update_fields=update_fields)


def persist_three_d_selection(project, version):
    selection_changed = project.selected_three_d_id != version.id
    project.selected_three_d = version
    project.step_2_completed_at = project.step_2_completed_at or timezone.now()
    update_fields = ['selected_three_d', 'step_2_completed_at', 'updated_at']
    if selection_changed:
        project.selected_boq = None
        project.step_3_completed_at = None
        project.boq_skipped_at = None
        project.completed_at = None
        update_fields.extend(
            [
                'selected_boq',
                'step_3_completed_at',
                'boq_skipped_at',
                'completed_at',
            ]
        )
    project.save(update_fields=update_fields)


class ProjectListCreateAPIView(APIView):
    def get(self, request):
        projects = Project.objects.filter(owner=request.user)
        return Response(ProjectSerializer(projects, many=True).data)

    def post(self, request):
        serializer = ProjectSerializer(
            data=request.data,
            context={'request': request},
        )
        serializer.is_valid(raise_exception=True)
        try:
            project = serializer.save(owner=request.user)
        except IntegrityError as exc:
            raise ValidationError(
                {'name': 'You already have a project with this name.'}
            ) from exc
        return Response(
            ProjectSerializer(project).data,
            status=status.HTTP_201_CREATED,
        )


class ProjectDetailAPIView(APIView):
    def get(self, request, project_id):
        project = owned_project(request, project_id)
        return Response(ProjectSerializer(project).data)

    def patch(self, request, project_id):
        project = owned_project(request, project_id)
        serializer = ProjectSerializer(
            project,
            data=request.data,
            partial=True,
            context={'request': request},
        )
        serializer.is_valid(raise_exception=True)
        try:
            serializer.save()
        except IntegrityError as exc:
            raise ValidationError(
                {'name': 'You already have a project with this name.'}
            ) from exc
        return Response(serializer.data)

    def delete(self, request, project_id):
        project = owned_project(request, project_id)
        delete_project_files(project)
        project.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class FloorPlanHistoryAPIView(APIView):
    def get(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 1)
        versions = project.floor_plan_versions.select_related(
            'image',
            'mask',
            'prompt_message',
            'job',
            'project',
        ).prefetch_related('prompt_message__attachments')
        return Response(
            FloorPlanVersionSerializer(
                versions,
                many=True,
                context={'request': request},
            ).data
        )


class FloorPlanConversationAPIView(APIView):
    def get(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 1)
        conversation = Conversation.objects.filter(
            project=project,
            kind=Conversation.FLOOR_PLAN,
        ).first()
        if conversation is None:
            return Response([])
        messages = conversation.messages.prefetch_related('attachments')
        return Response(
            ConversationMessageSerializer(
                messages,
                many=True,
                context={'request': request},
            ).data
        )


class FloorPlanUploadAPIView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 1)
        uploaded_file = request.FILES.get('file')
        if uploaded_file is None:
            raise ValidationError({'file': 'A 2D floor-plan file is required.'})
        validate_floor_plan_upload(uploaded_file)

        with transaction.atomic():
            asset = create_asset(
                project,
                request.user,
                uploaded_file,
                ProjectAsset.FLOOR_PLAN,
            )
            version = FloorPlanVersion.objects.create(
                project=project,
                created_by=request.user,
                source=FloorPlanVersion.UPLOADED,
                image=asset,
                status=ProcessingJob.COMPLETED,
                completed_at=timezone.now(),
            )
            persist_floor_plan_selection(project, version)

        return Response(
            FloorPlanVersionSerializer(
                version,
                context={'request': request},
            ).data,
            status=status.HTTP_201_CREATED,
        )


class StepTwoInputUploadAPIView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 2)
        if project.workflow != Project.STEP_2_ONLY:
            raise ValidationError(
                {'workflow': 'Complete workflows receive their 2D input from Step 1.'}
            )

        uploaded_file = request.FILES.get('file')
        if uploaded_file is None:
            raise ValidationError({'file': 'A 2D floor-plan file is required.'})
        validate_floor_plan_upload(uploaded_file)

        with transaction.atomic():
            asset = create_asset(
                project,
                request.user,
                uploaded_file,
                ProjectAsset.FLOOR_PLAN,
            )
            version = FloorPlanVersion.objects.create(
                project=project,
                created_by=request.user,
                source=FloorPlanVersion.UPLOADED,
                image=asset,
                status=ProcessingJob.COMPLETED,
                completed_at=timezone.now(),
            )
            project.selected_floor_plan = version
            project.save(update_fields=['selected_floor_plan', 'updated_at'])

        return Response(
            FloorPlanVersionSerializer(
                version,
                context={'request': request},
            ).data,
            status=status.HTTP_201_CREATED,
        )


class FloorPlanGenerateAPIView(APIView):
    def post(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 1)
        serializer = PromptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        parent = None
        parent_id = serializer.validated_data.get('parent_version_id')
        if parent_id:
            parent = get_object_or_404(
                FloorPlanVersion,
                id=parent_id,
                project=project,
                status=ProcessingJob.COMPLETED,
            )

        job_type = (
            ProcessingJob.FLOOR_PLAN_EDIT
            if parent
            else ProcessingJob.FLOOR_PLAN_GENERATE
        )
        source = FloorPlanVersion.EDITED if parent else FloorPlanVersion.GENERATED

        conversation = conversation_for(project, Conversation.FLOOR_PLAN)
        with transaction.atomic():
            message = ConversationMessage.objects.create(
                conversation=conversation,
                role=ConversationMessage.USER,
                content=serializer.validated_data['prompt'],
                metadata={
                    'parent_version_id': str(parent.id) if parent else None,
                },
            )
            if parent and parent.image_id:
                message.attachments.add(parent.image)
            job = create_job(project, request.user, job_type)
            version = FloorPlanVersion.objects.create(
                project=project,
                created_by=request.user,
                source=source,
                prompt=serializer.validated_data['prompt'],
                instruction=serializer.validated_data['prompt'] if parent else '',
                prompt_message=message,
                parent=parent,
                job=job,
            )

        enqueue_job(job)
        return Response(
            FloorPlanVersionSerializer(
                version,
                context={'request': request},
            ).data,
            status=status.HTTP_202_ACCEPTED,
        )


class FloorPlanEditAPIView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 1)
        serializer = FloorPlanEditSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validate_image_upload(serializer.validated_data['mask'], field_name='mask')
        original = get_object_or_404(
            FloorPlanVersion,
            id=serializer.validated_data['original_version_id'],
            project=project,
            status=ProcessingJob.COMPLETED,
            image__isnull=False,
        )

        conversation = conversation_for(project, Conversation.FLOOR_PLAN)
        with transaction.atomic():
            mask = create_asset(
                project,
                request.user,
                serializer.validated_data['mask'],
                ProjectAsset.MASK,
            )
            instruction = serializer.validated_data['instruction']
            message = ConversationMessage.objects.create(
                conversation=conversation,
                role=ConversationMessage.USER,
                content=instruction,
                metadata={'original_version_id': str(original.id)},
            )
            message.attachments.add(original.image, mask)
            job = create_job(project, request.user, ProcessingJob.FLOOR_PLAN_EDIT)
            version = FloorPlanVersion.objects.create(
                project=project,
                created_by=request.user,
                source=FloorPlanVersion.EDITED,
                prompt=instruction,
                instruction=instruction,
                prompt_message=message,
                parent=original,
                mask=mask,
                job=job,
            )

        enqueue_job(job)
        return Response(
            FloorPlanVersionSerializer(
                version,
                context={'request': request},
            ).data,
            status=status.HTTP_202_ACCEPTED,
        )


class FloorPlanSelectAPIView(APIView):
    def post(self, request, project_id, version_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 1)
        version = get_object_or_404(
            FloorPlanVersion,
            id=version_id,
            project=project,
            status=ProcessingJob.COMPLETED,
            image__isnull=False,
        )
        persist_floor_plan_selection(project, version)
        return Response(ProjectSerializer(project).data)


class ThreeDHistoryAPIView(APIView):
    def get(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 2)
        versions = project.three_d_versions.select_related(
            'project',
            'prompt_message',
            'image',
            'mask',
            'job',
        ).prefetch_related('prompt_message__attachments')
        return Response(
            ThreeDVersionSerializer(
                versions,
                many=True,
                context={'request': request},
            ).data
        )


class ThreeDConversationAPIView(APIView):
    def get(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 2)
        conversation = Conversation.objects.filter(
            project=project,
            kind=Conversation.THREE_D,
        ).first()
        if conversation is None:
            return Response([])
        messages = conversation.messages.prefetch_related('attachments')
        return Response(
            ConversationMessageSerializer(
                messages,
                many=True,
                context={'request': request},
            ).data
        )


class ThreeDGenerateAPIView(APIView):
    def post(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 2)
        serializer = ThreeDGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        floor_plan_id = serializer.validated_data.get('floor_plan_version_id')
        if floor_plan_id:
            floor_plan = get_object_or_404(
                FloorPlanVersion,
                id=floor_plan_id,
                project=project,
                status=ProcessingJob.COMPLETED,
                image__isnull=False,
            )
        else:
            floor_plan = effective_floor_plan(project)
        if floor_plan is None:
            raise ValidationError(
                {'floor_plan': 'A completed 2D floor plan is required for Step 2.'}
            )

        if project.workflow == Project.COMPLETE and project.selected_floor_plan_id is None:
            persist_floor_plan_selection(project, floor_plan)

        conversation = conversation_for(project, Conversation.THREE_D)
        with transaction.atomic():
            render_style = serializer.validated_data['render_style']
            message = ConversationMessage.objects.create(
                conversation=conversation,
                role=ConversationMessage.USER,
                content=serializer.validated_data['prompt'],
                metadata={
                    'floor_plan_version_id': str(floor_plan.id),
                    'render_style': render_style,
                },
            )
            message.attachments.add(floor_plan.image)
            job = create_job(project, request.user, ProcessingJob.THREE_D_GENERATE)
            version = ThreeDVersion.objects.create(
                project=project,
                created_by=request.user,
                source=ThreeDVersion.GENERATED,
                render_style=render_style,
                prompt_message=message,
                floor_plan=floor_plan,
                job=job,
            )

        enqueue_job(job)
        return Response(
            ThreeDVersionSerializer(
                version,
                context={'request': request},
            ).data,
            status=status.HTTP_202_ACCEPTED,
        )


class ThreeDEditAPIView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 2)
        serializer = ThreeDEditSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validate_image_upload(serializer.validated_data['mask'], field_name='mask')
        original = get_object_or_404(
            ThreeDVersion,
            id=serializer.validated_data['original_version_id'],
            project=project,
            status=ProcessingJob.COMPLETED,
            image__isnull=False,
        )

        conversation = conversation_for(project, Conversation.THREE_D)
        with transaction.atomic():
            mask = create_asset(
                project,
                request.user,
                serializer.validated_data['mask'],
                ProjectAsset.MASK,
            )
            message = ConversationMessage.objects.create(
                conversation=conversation,
                role=ConversationMessage.USER,
                content=serializer.validated_data['instruction'],
            )
            message.attachments.add(original.image, mask)
            job = create_job(project, request.user, ProcessingJob.THREE_D_EDIT)
            version = ThreeDVersion.objects.create(
                project=project,
                created_by=request.user,
                source=ThreeDVersion.EDITED,
                render_style=original.render_style,
                angle=original.angle,
                prompt_message=message,
                floor_plan=original.floor_plan,
                parent=original,
                mask=mask,
                instruction=serializer.validated_data['instruction'],
                job=job,
            )

        enqueue_job(job)
        return Response(
            ThreeDVersionSerializer(
                version,
                context={'request': request},
            ).data,
            status=status.HTTP_202_ACCEPTED,
        )


class ThreeDAngleAPIView(APIView):
    def post(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 2)
        serializer = ThreeDAngleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        original = get_object_or_404(
            ThreeDVersion,
            id=serializer.validated_data['original_version_id'],
            project=project,
            status=ProcessingJob.COMPLETED,
            image__isnull=False,
        )

        angle = serializer.validated_data['angle']
        conversation = conversation_for(project, Conversation.THREE_D)
        with transaction.atomic():
            message = ConversationMessage.objects.create(
                conversation=conversation,
                role=ConversationMessage.USER,
                content=f'Generate the {angle} viewing angle.',
                metadata={
                    'original_version_id': str(original.id),
                    'angle': angle,
                },
            )
            message.attachments.add(original.image)
            job = create_job(
                project,
                request.user,
                ProcessingJob.THREE_D_ANGLE,
                parameters={'angle': angle},
            )
            version = ThreeDVersion.objects.create(
                project=project,
                created_by=request.user,
                source=ThreeDVersion.ANGLE,
                render_style=original.render_style,
                angle=angle,
                prompt_message=message,
                floor_plan=original.floor_plan,
                parent=original,
                instruction=message.content,
                job=job,
            )

        enqueue_job(job)
        return Response(
            ThreeDVersionSerializer(
                version,
                context={'request': request},
            ).data,
            status=status.HTTP_202_ACCEPTED,
        )


class ThreeDSelectAPIView(APIView):
    def post(self, request, project_id, version_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 2)
        version = get_object_or_404(
            ThreeDVersion,
            id=version_id,
            project=project,
            status=ProcessingJob.COMPLETED,
            image__isnull=False,
        )
        persist_three_d_selection(project, version)
        return Response(ProjectSerializer(project).data)


class BOQConversationAPIView(APIView):
    parser_classes = [JSONParser]

    def get(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 3)
        conversation = Conversation.objects.filter(
            project=project,
            kind=Conversation.BOQ,
        ).first()
        if conversation is None:
            return Response([])
        messages = conversation.messages.prefetch_related('attachments')
        return Response(
            ConversationMessageSerializer(
                messages,
                many=True,
                context={'request': request},
            ).data
        )

    def post(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 3)
        raw_content = request.data.get('content', '')
        if not isinstance(raw_content, str):
            raise ValidationError({'content': 'Message content must be text.'})
        content = raw_content.strip()
        if not content:
            raise ValidationError({'content': 'Message content is required.'})

        conversation = conversation_for(project, Conversation.BOQ)
        with transaction.atomic():
            message = ConversationMessage.objects.create(
                conversation=conversation,
                role=ConversationMessage.USER,
                content=content,
            )

        return Response(
            ConversationMessageSerializer(
                message,
                context={'request': request},
            ).data,
            status=status.HTTP_201_CREATED,
        )


class BOQGenerateAPIView(APIView):
    parser_classes = [JSONParser]

    def post(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 3)
        serializer = BOQGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if project.workflow == Project.COMPLETE:
            three_d = effective_three_d(project)
            if three_d is None:
                raise ValidationError(
                    {'three_d': 'A completed 3D image is required for Step 3.'}
                )
            if project.selected_three_d_id is None:
                persist_three_d_selection(project, three_d)

        conversation = conversation_for(project, Conversation.BOQ)
        with transaction.atomic():
            document_ids = list(
                project.documents.values_list('id', flat=True)
            )
            message = ConversationMessage.objects.create(
                conversation=conversation,
                role=ConversationMessage.USER,
                content=serializer.validated_data['prompt'],
                metadata={
                    'document_ids': [str(document_id) for document_id in document_ids],
                    'three_d_version_id': (
                        str(project.selected_three_d_id)
                        if project.selected_three_d_id
                        else None
                    ),
                },
            )

            job = create_job(project, request.user, ProcessingJob.BOQ_GENERATE)
            version = BOQVersion.objects.create(
                project=project,
                created_by=request.user,
                version_number=next_boq_version_number(project),
                source=BOQVersion.GENERATED,
                source_message=message,
                job=job,
            )

        enqueue_job(job)
        return Response(
            BOQVersionSerializer(version).data,
            status=status.HTTP_202_ACCEPTED,
        )


class BOQVersionListAPIView(APIView):
    def get(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 3)
        return Response(
            BOQVersionSerializer(project.boq_versions.select_related('job'), many=True).data
        )


class BOQManualVersionAPIView(APIView):
    def post(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 3)
        serializer = BOQManualVersionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        parent = None
        parent_id = serializer.validated_data.get('parent_version_id')
        if parent_id:
            parent = get_object_or_404(
                BOQVersion,
                id=parent_id,
                project=project,
                status=ProcessingJob.COMPLETED,
            )

        with transaction.atomic():
            version = BOQVersion.objects.create(
                project=project,
                created_by=request.user,
                version_number=next_boq_version_number(project),
                source=BOQVersion.MANUAL,
                parent=parent,
                structured_data=serializer.validated_data['structured_data'],
                status=ProcessingJob.COMPLETED,
                completed_at=timezone.now(),
            )

        return Response(
            BOQVersionSerializer(version).data,
            status=status.HTTP_201_CREATED,
        )


class BOQSelectAPIView(APIView):
    def post(self, request, project_id, version_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 3)
        version = get_object_or_404(
            BOQVersion,
            id=version_id,
            project=project,
            status=ProcessingJob.COMPLETED,
        )
        project.selected_boq = version
        project.step_3_completed_at = timezone.now()
        project.boq_skipped_at = None
        project.completed_at = None
        project.save(
            update_fields=[
                'selected_boq',
                'step_3_completed_at',
                'boq_skipped_at',
                'completed_at',
                'updated_at',
            ]
        )
        return Response(ProjectSerializer(project).data)


class BOQSkipAPIView(APIView):
    def post(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 3)
        project.selected_boq = None
        project.step_3_completed_at = None
        project.boq_skipped_at = timezone.now()
        project.completed_at = None
        project.save(
            update_fields=[
                'selected_boq',
                'step_3_completed_at',
                'boq_skipped_at',
                'completed_at',
                'updated_at',
            ]
        )
        return Response(ProjectSerializer(project).data)


class ConversationMessageDeleteAPIView(APIView):
    def delete(self, request, project_id, message_id):
        project = owned_project(request, project_id)
        message = get_object_or_404(
            ConversationMessage,
            id=message_id,
            conversation__project=project,
        )
        message.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProjectDocumentListCreateAPIView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 3)
        documents = project.documents.select_related('asset')
        return Response(
            ProjectDocumentSerializer(
                documents,
                many=True,
                context={'request': request},
            ).data
        )

    def post(self, request, project_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 3)
        serializer = ProjectDocumentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        uploaded_file = serializer.validated_data['file']
        validate_upload(uploaded_file)

        with transaction.atomic():
            asset = create_asset(
                project,
                request.user,
                uploaded_file,
                ProjectAsset.DOCUMENT,
            )
            document = ProjectDocument.objects.create(
                project=project,
                asset=asset,
                title=serializer.validated_data.get('title') or asset.original_name,
                document_type=serializer.validated_data['document_type'],
            )

        return Response(
            ProjectDocumentSerializer(
                document,
                context={'request': request},
            ).data,
            status=status.HTTP_201_CREATED,
        )


class ProjectDocumentDetailAPIView(APIView):
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_document(self, project, document_id):
        return get_object_or_404(
            ProjectDocument.objects.select_related('asset'),
            id=document_id,
            project=project,
        )

    def get(self, request, project_id, document_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 3)
        document = self.get_document(project, document_id)
        return Response(
            ProjectDocumentSerializer(
                document,
                context={'request': request},
            ).data
        )

    def patch(self, request, project_id, document_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 3)
        document = self.get_document(project, document_id)
        serializer = ProjectDocumentUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        old_asset = None
        with transaction.atomic():
            uploaded_file = serializer.validated_data.get('file')
            if uploaded_file is not None:
                validate_upload(uploaded_file)
                old_asset = document.asset
                document.asset = create_asset(
                    project,
                    request.user,
                    uploaded_file,
                    ProjectAsset.DOCUMENT,
                )

            if 'title' in serializer.validated_data:
                document.title = serializer.validated_data['title']
            if 'document_type' in serializer.validated_data:
                document.document_type = serializer.validated_data['document_type']
            document.save()

        if old_asset is not None:
            old_asset.file.delete(save=False)
            old_asset.delete()

        return Response(
            ProjectDocumentSerializer(
                document,
                context={'request': request},
            ).data
        )

    def delete(self, request, project_id, document_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 3)
        document = self.get_document(project, document_id)
        asset = document.asset
        document.delete()
        asset.file.delete(save=False)
        asset.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class BOQCSVDownloadAPIView(APIView):
    def get(self, request, project_id, version_id):
        project = owned_project(request, project_id)
        ensure_step_allowed(project, 3)
        version = get_object_or_404(
            BOQVersion,
            id=version_id,
            project=project,
            status=ProcessingJob.COMPLETED,
        )

        data = version.structured_data
        if isinstance(data, dict):
            columns = data.get('columns', [])
            rows = data.get('rows', [])
        else:
            rows = data
            columns = []

        if not isinstance(rows, list):
            raise ValidationError(
                {'structured_data': 'BOQ rows must be a list to export CSV.'}
            )
        if not columns and rows and isinstance(rows[0], dict):
            columns = list(rows[0].keys())
        if not isinstance(columns, list):
            raise ValidationError(
                {'structured_data': 'BOQ columns must be a list to export CSV.'}
            )

        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = (
            f'attachment; filename="boq-version-{version.version_number}.csv"'
        )
        writer = csv.writer(response)
        writer.writerow([safe_csv_value(column) for column in columns])
        for row in rows:
            if isinstance(row, dict):
                values = [row.get(column, '') for column in columns]
            elif isinstance(row, list):
                values = row
            else:
                values = [row]
            writer.writerow([safe_csv_value(value) for value in values])
        return response


def safe_csv_value(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        value = json.dumps(value)
    text = str(value)
    if text.startswith(('=', '+', '-', '@')):
        return f"'{text}"
    return text


class ProjectOutputAPIView(APIView):
    def get(self, request, project_id):
        project = owned_project(request, project_id)
        floor_plans = project.floor_plan_versions.filter(
            status=ProcessingJob.COMPLETED,
            image__isnull=False,
        ).select_related(
            'project',
            'image',
            'mask',
            'prompt_message',
            'job',
        ).prefetch_related('prompt_message__attachments')
        three_d_versions = project.three_d_versions.filter(
            status=ProcessingJob.COMPLETED,
            image__isnull=False,
        ).select_related(
            'project',
            'prompt_message',
            'image',
            'mask',
            'job',
        ).prefetch_related('prompt_message__attachments')
        boq_versions = project.boq_versions.filter(
            status=ProcessingJob.COMPLETED,
        ).select_related('project', 'job')
        documents = project.documents.select_related('asset')

        floor_plan_data = FloorPlanVersionSerializer(
            floor_plans,
            many=True,
            context={'request': request},
        ).data
        three_d_data = ThreeDVersionSerializer(
            three_d_versions,
            many=True,
            context={'request': request},
        ).data
        boq_data = BOQVersionSerializer(boq_versions, many=True).data
        document_data = ProjectDocumentSerializer(
            documents,
            many=True,
            context={'request': request},
        ).data

        return Response(
            {
                'project': ProjectSerializer(project).data,
                'summary': {
                    'total_deliverables': (
                        len(floor_plan_data)
                        + len(three_d_data)
                        + len(boq_data)
                        + len(document_data)
                    ),
                    'floor_plans': len(floor_plan_data),
                    'three_d_renders': len(three_d_data),
                    'boq_versions': len(boq_data),
                    'documents': len(document_data),
                },
                'floor_plans': floor_plan_data,
                'three_d_renders': three_d_data,
                'boq_versions': boq_data,
                'documents': document_data,
            }
        )


class ProjectFinishAPIView(APIView):
    def post(self, request, project_id):
        project = owned_project(request, project_id)
        errors = {}

        if project.workflow in {Project.COMPLETE, Project.STEP_1_ONLY}:
            if project.selected_floor_plan_id is None:
                errors['floor_plan'] = 'Approve a 2D floor plan before finishing.'
        if project.workflow in {Project.COMPLETE, Project.STEP_2_ONLY}:
            if project.selected_three_d_id is None:
                errors['three_d'] = 'Approve a 3D render before finishing.'
        if project.workflow in {Project.COMPLETE, Project.STEP_3_ONLY}:
            if project.selected_boq_id is None and project.boq_skipped_at is None:
                errors['boq'] = 'Approve a BOQ or explicitly skip it before finishing.'

        if errors:
            raise ValidationError(errors)

        project.completed_at = timezone.now()
        project.save(update_fields=['completed_at', 'updated_at'])
        return Response(ProjectSerializer(project).data)


class ProjectAssetListAPIView(APIView):
    def get(self, request, project_id):
        project = owned_project(request, project_id)
        assets = project.assets.all()
        kind = request.query_params.get('kind')
        if kind:
            valid_kinds = {choice[0] for choice in ProjectAsset.KIND_CHOICES}
            if kind not in valid_kinds:
                raise ValidationError({'kind': 'Invalid asset kind.'})
            assets = assets.filter(kind=kind)
        return Response(
            ProjectAssetSerializer(
                assets,
                many=True,
                context={'request': request},
            ).data
        )


class ProjectAssetDownloadAPIView(APIView):
    def get(self, request, project_id, asset_id):
        project = owned_project(request, project_id)
        asset = get_object_or_404(ProjectAsset, id=asset_id, project=project)
        response = FileResponse(
            asset.file.open('rb'),
            as_attachment=True,
            filename=asset.original_name,
            content_type=asset.content_type or 'application/octet-stream',
        )
        return response


class ProjectArchiveAPIView(APIView):
    def post(self, request, project_id):
        project = owned_project(request, project_id)
        serializer = ProjectArchiveRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        scope = serializer.validated_data['scope']
        active_archive = project.jobs.filter(
            job_type=ProcessingJob.PROJECT_ARCHIVE,
            status__in=[ProcessingJob.QUEUED, ProcessingJob.PROCESSING],
            parameters__scope=scope,
        ).first()
        if active_archive:
            return Response(
                ProcessingJobSerializer(active_archive).data,
                status=status.HTTP_202_ACCEPTED,
            )

        job = create_job(
            project,
            request.user,
            ProcessingJob.PROJECT_ARCHIVE,
            parameters={'scope': scope},
        )
        enqueue_job(job)
        return Response(
            ProcessingJobSerializer(job).data,
            status=status.HTTP_202_ACCEPTED,
        )


class ProcessingJobDetailAPIView(APIView):
    def get(self, request, job_id):
        job = get_object_or_404(
            ProcessingJob,
            id=job_id,
            project__owner=request.user,
        )
        return Response(ProcessingJobSerializer(job).data)
