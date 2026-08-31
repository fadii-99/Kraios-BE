import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
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


TEST_CHANNEL_LAYERS = {
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}
}


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
    AI_PLACEHOLDER_DELAY_SECONDS=0,
    CHANNEL_LAYERS=TEST_CHANNEL_LAYERS,
)
class ProjectWorkflowAPITests(APITestCase):
    def setUp(self):
        self.media_directory = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media_directory.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(self.media_directory.cleanup)

        self.user = User.objects.create_user(
            email='architect@example.com',
            password='StrongPassword123!',
            full_name='Test Architect',
            firm_name='Test Firm',
            country='Pakistan',
            is_active=True,
        )
        self.other_user = User.objects.create_user(
            email='other@example.com',
            password='StrongPassword123!',
            full_name='Other Architect',
            firm_name='Other Firm',
            country='Pakistan',
            is_active=True,
        )
        self.client.force_authenticate(self.user)

    def create_project(self, workflow=Project.COMPLETE):
        response = self.client.post(
            reverse('projects:list-create'),
            {'name': 'Tower Project', 'workflow': workflow},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return Project.objects.get(id=response.data['id'])

    def test_project_defaults_to_complete_workflow(self):
        response = self.client.post(
            reverse('projects:list-create'),
            {'name': 'Default Workflow Project'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['workflow'], Project.COMPLETE)
        project = Project.objects.get(id=response.data['id'])
        self.assertEqual(project.workflow, Project.COMPLETE)
        self.assertTrue(project.allows_step(1))
        self.assertTrue(project.allows_step(2))
        self.assertTrue(project.allows_step(3))
        self.assertTrue(project.allows_step(4))

    def test_workflow_is_immutable(self):
        project = self.create_project()
        response = self.client.patch(
            reverse('projects:detail', args=[project.id]),
            {'workflow': Project.STEP_1_ONLY},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        project.refresh_from_db()
        self.assertEqual(project.workflow, Project.COMPLETE)

    def test_project_is_private_to_owner(self):
        project = Project.objects.create(
            owner=self.other_user,
            name='Private Project',
            workflow=Project.STEP_1_ONLY,
        )
        response = self.client.get(reverse('projects:detail', args=[project.id]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_complete_workflow_keeps_versions_and_builds_archive(self):
        project = self.create_project()
        floor_file = SimpleUploadedFile(
            'floor.png',
            b'floor-plan-content',
            content_type='image/png',
        )
        upload_response = self.client.post(
            reverse('projects:floor-plan-upload', args=[project.id]),
            {'file': floor_file},
            format='multipart',
        )
        self.assertEqual(upload_response.status_code, status.HTTP_201_CREATED)
        project.refresh_from_db()
        self.assertIsNotNone(project.selected_floor_plan_id)

        floor_generate_response = self.client.post(
            reverse('projects:floor-plan-generate', args=[project.id]),
            {'prompt': 'Create an open kitchen floor plan.'},
            format='json',
        )
        self.assertEqual(floor_generate_response.status_code, status.HTTP_202_ACCEPTED)
        floor_job = ProcessingJob.objects.get(id=floor_generate_response.data['job']['id'])
        self.assertEqual(floor_job.status, ProcessingJob.COMPLETED)

        three_d_response = self.client.post(
            reverse('projects:three-d-generate', args=[project.id]),
            {'prompt': 'Create a modern interior render.'},
            format='json',
        )
        self.assertEqual(three_d_response.status_code, status.HTTP_202_ACCEPTED)
        three_d_job = ProcessingJob.objects.get(id=three_d_response.data['job']['id'])
        self.assertEqual(three_d_job.status, ProcessingJob.COMPLETED)
        three_d_version = three_d_job.three_d_version

        select_response = self.client.post(
            reverse('projects:three-d-select', args=[project.id, three_d_version.id]),
        )
        self.assertEqual(select_response.status_code, status.HTTP_200_OK)

        boq_response = self.client.post(
            reverse('projects:boq-generate', args=[project.id]),
            {'prompt': 'Generate the complete BOQ.'},
            format='json',
        )
        self.assertEqual(boq_response.status_code, status.HTTP_202_ACCEPTED)
        boq_job = ProcessingJob.objects.get(id=boq_response.data['job']['id'])
        self.assertEqual(boq_job.status, ProcessingJob.COMPLETED)
        self.assertIn('columns', boq_job.boq_version.structured_data)

        manual_response = self.client.post(
            reverse('projects:boq-version-manual', args=[project.id]),
            {
                'parent_version_id': boq_job.boq_version.id,
                'structured_data': {'columns': ['Name'], 'rows': [{'Name': 'Wall'}]},
            },
            format='json',
        )
        self.assertEqual(manual_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(BOQVersion.objects.filter(project=project).count(), 2)

        archive_response = self.client.post(
            reverse('projects:download-all', args=[project.id]),
        )
        self.assertEqual(archive_response.status_code, status.HTTP_202_ACCEPTED)
        archive_job = ProcessingJob.objects.get(id=archive_response.data['id'])
        self.assertEqual(archive_job.status, ProcessingJob.COMPLETED)
        self.assertEqual(archive_job.output_asset.kind, ProjectAsset.ARCHIVE)

    def test_floor_plan_conversation_edit_and_message_delete(self):
        project = self.create_project()
        generate_response = self.client.post(
            reverse('projects:floor-plan-generate', args=[project.id]),
            {'prompt': 'Create a compact family house.'},
            format='json',
        )
        self.assertEqual(generate_response.status_code, status.HTTP_202_ACCEPTED)
        generated_version = FloorPlanVersion.objects.get(
            id=generate_response.data['id']
        )
        self.assertEqual(generated_version.status, ProcessingJob.COMPLETED)

        conversation_response = self.client.get(
            reverse('projects:floor-plan-conversation', args=[project.id])
        )
        self.assertEqual(conversation_response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [message['role'] for message in conversation_response.data],
            [ConversationMessage.USER, ConversationMessage.ASSISTANT],
        )

        mask = SimpleUploadedFile(
            'mask.png',
            b'mask-content',
            content_type='image/png',
        )
        edit_response = self.client.post(
            reverse('projects:floor-plan-edit', args=[project.id]),
            {
                'original_version_id': generated_version.id,
                'instruction': 'Expand the living room.',
                'mask': mask,
            },
            format='multipart',
        )
        self.assertEqual(edit_response.status_code, status.HTTP_202_ACCEPTED)
        edited_version = FloorPlanVersion.objects.get(id=edit_response.data['id'])
        self.assertEqual(edited_version.source, FloorPlanVersion.EDITED)
        self.assertEqual(edited_version.parent, generated_version)
        self.assertIsNotNone(edited_version.mask_id)

        prompt_message_id = edited_version.prompt_message_id
        delete_response = self.client.delete(
            reverse(
                'projects:message-delete',
                args=[project.id, prompt_message_id],
            )
        )
        self.assertEqual(delete_response.status_code, status.HTTP_204_NO_CONTENT)
        edited_version.refresh_from_db()
        self.assertIsNone(edited_version.prompt_message_id)

        assistant_message = ConversationMessage.objects.filter(
            conversation__project=project,
            conversation__kind=Conversation.FLOOR_PLAN,
            role=ConversationMessage.ASSISTANT,
        ).first()
        delete_assistant_response = self.client.delete(
            reverse(
                'projects:message-delete',
                args=[project.id, assistant_message.id],
            )
        )
        self.assertEqual(
            delete_assistant_response.status_code,
            status.HTTP_204_NO_CONTENT,
        )

    def test_three_d_style_angle_and_approval_flow(self):
        project = self.create_project()
        floor_file = SimpleUploadedFile(
            'floor.pdf',
            b'pdf-content',
            content_type='application/pdf',
        )
        upload_response = self.client.post(
            reverse('projects:floor-plan-upload', args=[project.id]),
            {'file': floor_file},
            format='multipart',
        )
        self.assertEqual(upload_response.status_code, status.HTTP_201_CREATED)

        generate_response = self.client.post(
            reverse('projects:three-d-generate', args=[project.id]),
            {
                'prompt': 'Create a modern furnished render.',
                'floor_plan_version_id': upload_response.data['id'],
                'render_style': ThreeDVersion.PHOTOREALISTIC,
            },
            format='json',
        )
        self.assertEqual(generate_response.status_code, status.HTTP_202_ACCEPTED)
        generated_version = ThreeDVersion.objects.get(id=generate_response.data['id'])
        self.assertEqual(generated_version.status, ProcessingJob.COMPLETED)
        self.assertEqual(
            generated_version.render_style,
            ThreeDVersion.PHOTOREALISTIC,
        )

        angle_response = self.client.post(
            reverse('projects:three-d-angle', args=[project.id]),
            {
                'original_version_id': generated_version.id,
                'angle': ThreeDVersion.ISOMETRIC_45,
            },
            format='json',
        )
        self.assertEqual(angle_response.status_code, status.HTTP_202_ACCEPTED)
        angle_version = ThreeDVersion.objects.get(id=angle_response.data['id'])
        self.assertEqual(angle_version.source, ThreeDVersion.ANGLE)
        self.assertEqual(angle_version.angle, ThreeDVersion.ISOMETRIC_45)
        self.assertEqual(angle_version.parent, generated_version)
        self.assertEqual(angle_version.status, ProcessingJob.COMPLETED)

        approve_response = self.client.post(
            reverse('projects:three-d-approve', args=[project.id, angle_version.id])
        )
        self.assertEqual(approve_response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            approve_response.data['workflow_state']['current_step'],
            3,
        )

        archive_response = self.client.post(
            reverse('projects:download-all', args=[project.id]),
            {'scope': 'THREE_D'},
            format='json',
        )
        self.assertEqual(archive_response.status_code, status.HTTP_202_ACCEPTED)
        archive_job = ProcessingJob.objects.get(id=archive_response.data['id'])
        self.assertEqual(archive_job.parameters['scope'], 'THREE_D')
        self.assertEqual(archive_job.output_asset.metadata['scope'], 'THREE_D')

    def test_boq_documents_are_separate_and_support_crud(self):
        project = self.create_project()
        document_file = SimpleUploadedFile(
            'brief.pdf',
            b'project-brief',
            content_type='application/pdf',
        )
        create_response = self.client.post(
            reverse('projects:document-list-create', args=[project.id]),
            {
                'file': document_file,
                'title': 'Initial brief',
                'document_type': ProjectDocument.PROJECT_BRIEF,
            },
            format='multipart',
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        document_id = create_response.data['id']
        self.assertEqual(ConversationMessage.objects.count(), 0)

        list_response = self.client.get(
            reverse('projects:document-list-create', args=[project.id])
        )
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_response.data), 1)

        update_response = self.client.patch(
            reverse('projects:document-detail', args=[project.id, document_id]),
            {
                'title': 'Updated project brief',
                'document_type': ProjectDocument.GENERAL,
            },
            format='json',
        )
        self.assertEqual(update_response.status_code, status.HTTP_200_OK)
        self.assertEqual(update_response.data['title'], 'Updated project brief')

        delete_response = self.client.delete(
            reverse('projects:document-detail', args=[project.id, document_id])
        )
        self.assertEqual(delete_response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ProjectDocument.objects.filter(id=document_id).exists())

    def test_boq_csv_output_and_project_finish(self):
        project = self.create_project()
        floor_file = SimpleUploadedFile(
            'floor.png',
            b'floor-plan-content',
            content_type='image/png',
        )
        self.client.post(
            reverse('projects:floor-plan-upload', args=[project.id]),
            {'file': floor_file},
            format='multipart',
        )
        three_d_response = self.client.post(
            reverse('projects:three-d-generate', args=[project.id]),
            {'prompt': 'Create a 3D render.'},
            format='json',
        )
        three_d_version = ThreeDVersion.objects.get(id=three_d_response.data['id'])
        self.client.post(
            reverse('projects:three-d-approve', args=[project.id, three_d_version.id])
        )

        boq_response = self.client.post(
            reverse('projects:boq-version-manual', args=[project.id]),
            {
                'structured_data': {
                    'columns': ['Item', 'Amount'],
                    'rows': [{'Item': 'Concrete', 'Amount': '=2+2'}],
                }
            },
            format='json',
        )
        self.assertEqual(boq_response.status_code, status.HTTP_201_CREATED)
        boq_id = boq_response.data['id']
        self.client.post(
            reverse('projects:boq-approve', args=[project.id, boq_id])
        )

        csv_response = self.client.get(
            reverse('projects:boq-download-csv', args=[project.id, boq_id])
        )
        self.assertEqual(csv_response.status_code, status.HTTP_200_OK)
        self.assertEqual(csv_response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn("'=2+2", csv_response.content.decode())

        finish_response = self.client.post(
            reverse('projects:finish', args=[project.id])
        )
        self.assertEqual(finish_response.status_code, status.HTTP_200_OK)
        self.assertTrue(finish_response.data['workflow_state']['is_finished'])

        output_response = self.client.get(
            reverse('projects:output', args=[project.id])
        )
        self.assertEqual(output_response.status_code, status.HTTP_200_OK)
        self.assertEqual(output_response.data['summary']['floor_plans'], 1)
        self.assertEqual(output_response.data['summary']['three_d_renders'], 1)
        self.assertEqual(output_response.data['summary']['boq_versions'], 1)

    def test_user_can_skip_boq_and_finish_complete_workflow(self):
        project = self.create_project()
        floor_file = SimpleUploadedFile(
            'floor.png',
            b'floor-plan-content',
            content_type='image/png',
        )
        self.client.post(
            reverse('projects:floor-plan-upload', args=[project.id]),
            {'file': floor_file},
            format='multipart',
        )
        three_d_response = self.client.post(
            reverse('projects:three-d-generate', args=[project.id]),
            {'prompt': 'Create a 3D render.'},
            format='json',
        )
        three_d_version = ThreeDVersion.objects.get(id=three_d_response.data['id'])
        self.client.post(
            reverse('projects:three-d-approve', args=[project.id, three_d_version.id])
        )

        skip_response = self.client.post(
            reverse('projects:boq-skip', args=[project.id])
        )
        self.assertEqual(skip_response.status_code, status.HTTP_200_OK)
        self.assertTrue(skip_response.data['workflow_state']['boq_skipped'])
        self.assertEqual(skip_response.data['workflow_state']['current_step'], 4)

        finish_response = self.client.post(
            reverse('projects:finish', args=[project.id])
        )
        self.assertEqual(finish_response.status_code, status.HTTP_200_OK)
