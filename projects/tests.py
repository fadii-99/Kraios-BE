import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from app.ai.floorplan_snapshot_renderer import CANVAS_H, CANVAS_W
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
from app.ai.boq import final_table_missing

from .ai_pipeline import (
    BOQPipelineOutput,
    FloorPlanAnalysisOutput,
    ImagePipelineOutput,
    _canonical_boq_header,
    _canonical_boq_table,
    _extract_markdown_boq,
    generate_three_d,
)
from .tasks import PLACEHOLDER_PNG


TEST_CHANNEL_LAYERS = {
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}
}

# Schema-valid FloorPlan JSON: the Step 2 adapter round-trips stored geometry
# through the FloorPlan model, so a stub payload is rejected.
FLOOR_PLAN_JSON = json.dumps(
    {
        'units': 'meters',
        'wall_height': 2.7,
        'description': 'Single-room studio extracted from the CAD plan.',
        'outline': [[0, 0], [4, 0], [4, 3], [0, 3]],
        'walls': [
            {'start': [0, 0], 'end': [4, 0]},
            {'start': [4, 0], 'end': [4, 3]},
            {'start': [4, 3], 'end': [0, 3]},
            {'start': [0, 3], 'end': [0, 0]},
        ],
        'rooms': [
            {'name': 'Living Room', 'polygon': [[0, 0], [4, 0], [4, 3], [0, 3]]}
        ],
    }
)


class BOQFinalTableGuardrailTests(unittest.TestCase):
    """Pure-function tests for the port of the developer's Aug 30 BOQ fix
    (cad-image-generation@f86fc02): the '## FINAL BOQ' output contract and the
    guard against grabbing the wrong table out of a Step 4 reply."""

    def test_final_table_missing_true_when_heading_has_no_rows(self):
        self.assertTrue(final_table_missing("## FINAL BOQ\n\nCompiling now..."))

    def test_final_table_missing_false_when_heading_has_a_table(self):
        reply = (
            "## FINAL BOQ\n\n"
            "| Item | Description | Qty | Unit | Rate | Amount |\n"
            "|---|---|---|---|---|---|\n"
            "| 1 | Concrete | 10 | m3 | 100 | 1000 |\n"
        )
        self.assertFalse(final_table_missing(reply))

    def test_final_table_missing_ignores_prose_mentions(self):
        self.assertFalse(final_table_missing("Shall I add VAT to the final BOQ?"))

    def test_extract_markdown_boq_skips_a_pre_marker_corrections_table(self):
        reply = (
            "Rate corrections:\n\n"
            "| Description | Quantity | Old Rate | New Rate |\n"
            "|---|---|---|---|\n"
            "| Concrete | 10 | 90 | 100 |\n\n"
            "## FINAL BOQ\n\n"
            "| Description | Quantity | Amount |\n"
            "|---|---|---|\n"
            "| Concrete | 10 | 1000 |\n"
        )
        columns, rows = _extract_markdown_boq(reply)
        self.assertEqual(columns, ['Description', 'Quantity', 'Amount'])
        self.assertEqual(rows, [{'Description': 'Concrete', 'Quantity': '10', 'Amount': '1000'}])

    def test_extract_markdown_boq_falls_back_without_a_marker(self):
        reply = (
            "| Description | Quantity | Amount |\n"
            "|---|---|---|\n"
            "| Concrete | 10 | 1000 |\n"
        )
        columns, rows = _extract_markdown_boq(reply)
        self.assertEqual(rows, [{'Description': 'Concrete', 'Quantity': '10', 'Amount': '1000'}])


class BOQCanonicalColumnTests(unittest.TestCase):
    """The agent titles its markdown columns differently from run to run, so a
    compiled BOQ rendered with empty Rate/Amount cells: the values were stored
    under "Rate (AED)"/"Amount (AED)" while the client reads "Rate"/"Amount".
    Editing a row then saved back only what the client could see, losing the
    real rates from that version."""

    def test_agent_headers_are_mapped_onto_the_canonical_columns(self):
        columns, rows, currency = _canonical_boq_table(
            ['Item No.', 'Description', 'Unit', 'Quantity', 'Rate (AED)', 'Amount (AED)', 'Remarks'],
            [
                {
                    'Item No.': '1',
                    'Description': 'Floor screed',
                    'Unit': 'm²',
                    'Quantity': '200',
                    'Rate (AED)': '45.00',
                    'Amount (AED)': '9,000.00',
                    'Remarks': 'Entire net floor area',
                }
            ],
        )

        self.assertEqual(
            columns,
            ['Item', 'Description', 'Quantity', 'Unit', 'Rate', 'Amount', 'Remarks'],
        )
        self.assertEqual(rows[0]['Item'], '1')
        self.assertEqual(rows[0]['Rate'], '45.00')
        self.assertEqual(rows[0]['Amount'], '9,000.00')
        self.assertEqual(rows[0]['Remarks'], 'Entire net floor area')
        # The currency lived only in the header; it must survive the rename.
        self.assertEqual(currency, 'AED')

    def test_column_synonyms(self):
        for raw, expected in (
            ('Item No.', 'Item'),
            ('S.No', 'Item'),
            ('Particulars', 'Description'),
            ('Qty', 'Quantity'),
            ('UOM', 'Unit'),
            ('Unit Rate', 'Rate'),
            ('Rate (USD)', 'Rate'),
            ('Total Amount', 'Amount'),
            ('Notes', 'Remarks'),
        ):
            self.assertEqual(_canonical_boq_header(raw)[0], expected, raw)

    def test_an_unrecognised_column_is_kept_rather_than_dropped(self):
        columns, rows, _ = _canonical_boq_table(
            ['Item No.', 'Description', 'Supplier Ref'],
            [{'Item No.': '1', 'Description': 'Tiles', 'Supplier Ref': 'SUP-9'}],
        )

        self.assertEqual(columns, ['Item', 'Description', 'Supplier Ref'])
        self.assertEqual(rows[0]['Supplier Ref'], 'SUP-9')

    def test_two_columns_mapping_to_one_keep_the_populated_value(self):
        _, rows, _ = _canonical_boq_table(
            ['Rate', 'Unit Rate'],
            [{'Rate': '', 'Unit Rate': '99'}],
        )

        self.assertEqual(rows[0]['Rate'], '99')

    def test_nothing_to_canonicalise_passes_through(self):
        self.assertEqual(_canonical_boq_table([], []), ([], [], None))


class BOQDocumentExtractionTests(unittest.TestCase):
    """Port of cad-image-generation@5495d40's DXF/Excel context-assembly step:
    the BOQ prompt promises "DXF drawings ARE readable... block counts appear
    in the context", so the adapter must actually extract that text rather
    than just listing the file path."""

    def test_extract_excel_summary_reads_cell_contents(self):
        import openpyxl

        from projects.ai_pipeline import _extract_excel_summary

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'schedule.xlsx'
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = 'Doors'
            sheet.append(['Mark', 'Type', 'Width', 'Count'])
            sheet.append(['D1', 'Flush door', 900, 12])
            workbook.save(path)

            summary = _extract_excel_summary(str(path))

        self.assertIn('[Sheet: Doors]', summary)
        self.assertIn('D1 | Flush door | 900 | 12', summary)

    def test_extract_dxf_summary_reads_block_counts(self):
        import ezdxf

        from projects.ai_pipeline import _extract_dxf_summary

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'plan.dxf'
            doc = ezdxf.new()
            doc.blocks.new(name='DOOR_900')
            modelspace = doc.modelspace()
            for _ in range(3):
                modelspace.add_blockref('DOOR_900', (0, 0))
            doc.saveas(path)

            summary = _extract_dxf_summary(str(path))

        self.assertIn('DOOR_900 x3', summary)

    def test_extract_excel_summary_fails_open_on_a_corrupt_file(self):
        from projects.ai_pipeline import _extract_excel_summary

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'broken.xlsx'
            path.write_bytes(b'not a real workbook')
            summary = _extract_excel_summary(str(path))

        self.assertIn('Could not read this Excel file', summary)


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
    AI_PIPELINE_ENABLED=False,
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

        # Deleting the prompt message deletes the whole block it produced —
        # the version, its job, and its mask/image assets — not just the
        # message row with the version left dangling.
        prompt_message_id = edited_version.prompt_message_id
        edited_version_id = edited_version.id
        edited_job_id = edited_version.job_id
        edited_image_id = edited_version.image_id
        edited_mask_id = edited_version.mask_id
        delete_response = self.client.delete(
            reverse(
                'projects:message-delete',
                args=[project.id, prompt_message_id],
            )
        )
        self.assertEqual(delete_response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            delete_response.data,
            {
                'success': True,
                'message': 'Message block deleted successfully',
                'deleted_message_id': str(prompt_message_id),
            },
        )
        self.assertFalse(
            FloorPlanVersion.objects.filter(id=edited_version_id).exists()
        )
        self.assertFalse(
            ProcessingJob.objects.filter(id=edited_job_id).exists()
        )
        self.assertFalse(
            ProjectAsset.objects.filter(id=edited_mask_id).exists()
        )
        # The edited version's own generated image is exclusive to it and
        # must go too; the ORIGINAL version it was edited from must survive
        # untouched, since only the edit's own block was deleted.
        self.assertFalse(
            ProjectAsset.objects.filter(id=edited_image_id).exists()
        )
        self.assertTrue(
            FloorPlanVersion.objects.filter(id=generated_version.id).exists()
        )
        self.assertTrue(
            ProjectAsset.objects.filter(id=generated_version.image_id).exists()
        )

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
            status.HTTP_200_OK,
        )
        self.assertFalse(
            ConversationMessage.objects.filter(id=assistant_message.id).exists()
        )

    def test_floor_plan_retry_reuses_failed_user_message(self):
        project = self.create_project(Project.STEP_1_ONLY)
        conversation = Conversation.objects.create(
            project=project,
            kind=Conversation.FLOOR_PLAN,
        )
        message = ConversationMessage.objects.create(
            conversation=conversation,
            role=ConversationMessage.USER,
            content='Create a compact family house.',
        )
        failed_job = ProcessingJob.objects.create(
            project=project,
            created_by=self.user,
            job_type=ProcessingJob.FLOOR_PLAN_GENERATE,
            status=ProcessingJob.FAILED,
        )
        failed_version = FloorPlanVersion.objects.create(
            project=project,
            created_by=self.user,
            source=FloorPlanVersion.GENERATED,
            prompt='Create a compact family house.',
            prompt_message=message,
            job=failed_job,
            status=ProcessingJob.FAILED,
        )

        response = self.client.post(
            reverse('projects:floor-plan-generate', args=[project.id]),
            {
                'prompt': 'Create a compact family house.',
                'retry_message_id': message.id,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(
            str(response.data['prompt_message']['id']),
            str(message.id),
        )
        self.assertFalse(
            FloorPlanVersion.objects.filter(id=failed_version.id).exists()
        )
        self.assertFalse(ProcessingJob.objects.filter(id=failed_job.id).exists())
        self.assertEqual(FloorPlanVersion.objects.filter(project=project).count(), 1)
        self.assertEqual(
            ConversationMessage.objects.filter(
                conversation=conversation,
                role=ConversationMessage.USER,
            ).count(),
            1,
        )

    def test_three_d_retry_reuses_failed_user_message(self):
        project = self.create_project(Project.STEP_2_ONLY)
        floor_plan_asset = ProjectAsset.objects.create(
            project=project,
            uploaded_by=self.user,
            kind=ProjectAsset.FLOOR_PLAN,
            file=SimpleUploadedFile(
                'floor.png',
                PLACEHOLDER_PNG,
                content_type='image/png',
            ),
            original_name='floor.png',
            content_type='image/png',
            size=len(PLACEHOLDER_PNG),
        )
        floor_plan = FloorPlanVersion.objects.create(
            project=project,
            created_by=self.user,
            source=FloorPlanVersion.UPLOADED,
            image=floor_plan_asset,
            status=ProcessingJob.COMPLETED,
        )
        conversation = Conversation.objects.create(
            project=project,
            kind=Conversation.THREE_D,
        )
        message = ConversationMessage.objects.create(
            conversation=conversation,
            role=ConversationMessage.USER,
            content='Create a modern interior render.',
        )
        failed_job = ProcessingJob.objects.create(
            project=project,
            created_by=self.user,
            job_type=ProcessingJob.THREE_D_GENERATE,
            status=ProcessingJob.FAILED,
        )
        failed_version = ThreeDVersion.objects.create(
            project=project,
            created_by=self.user,
            source=ThreeDVersion.GENERATED,
            prompt_message=message,
            floor_plan=floor_plan,
            job=failed_job,
            status=ProcessingJob.FAILED,
        )

        response = self.client.post(
            reverse('projects:three-d-generate', args=[project.id]),
            {
                'prompt': 'Create a modern interior render.',
                'floor_plan_version_id': floor_plan.id,
                'retry_message_id': message.id,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(
            str(response.data['prompt_message']['id']),
            str(message.id),
        )
        self.assertFalse(
            ThreeDVersion.objects.filter(id=failed_version.id).exists()
        )
        self.assertFalse(ProcessingJob.objects.filter(id=failed_job.id).exists())
        self.assertEqual(ThreeDVersion.objects.filter(project=project).count(), 1)
        self.assertEqual(
            ConversationMessage.objects.filter(
                conversation=conversation,
                role=ConversationMessage.USER,
            ).count(),
            1,
        )

    def test_boq_retry_reuses_failed_user_message(self):
        project = self.create_project(Project.STEP_3_ONLY)
        conversation = Conversation.objects.create(
            project=project,
            kind=Conversation.BOQ,
        )
        message = ConversationMessage.objects.create(
            conversation=conversation,
            role=ConversationMessage.USER,
            content='Generate the complete BOQ.',
        )
        failed_job = ProcessingJob.objects.create(
            project=project,
            created_by=self.user,
            job_type=ProcessingJob.BOQ_GENERATE,
            status=ProcessingJob.FAILED,
        )
        failed_version = BOQVersion.objects.create(
            project=project,
            created_by=self.user,
            version_number=1,
            source=BOQVersion.GENERATED,
            source_message=message,
            job=failed_job,
            status=ProcessingJob.FAILED,
        )

        response = self.client.post(
            reverse('projects:boq-generate', args=[project.id]),
            {
                'prompt': 'Generate the complete BOQ.',
                'retry_message_id': message.id,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(str(response.data['source_message']), str(message.id))
        self.assertFalse(BOQVersion.objects.filter(id=failed_version.id).exists())
        self.assertFalse(ProcessingJob.objects.filter(id=failed_job.id).exists())
        self.assertEqual(BOQVersion.objects.filter(project=project).count(), 1)
        self.assertEqual(
            ConversationMessage.objects.filter(
                conversation=conversation,
                role=ConversationMessage.USER,
            ).count(),
            1,
        )

    def test_retry_rejects_a_message_without_a_failed_attempt(self):
        project = self.create_project(Project.STEP_1_ONLY)
        conversation = Conversation.objects.create(
            project=project,
            kind=Conversation.FLOOR_PLAN,
        )
        message = ConversationMessage.objects.create(
            conversation=conversation,
            role=ConversationMessage.USER,
            content='Create a compact family house.',
        )
        job = ProcessingJob.objects.create(
            project=project,
            created_by=self.user,
            job_type=ProcessingJob.FLOOR_PLAN_GENERATE,
            status=ProcessingJob.COMPLETED,
        )
        FloorPlanVersion.objects.create(
            project=project,
            created_by=self.user,
            source=FloorPlanVersion.GENERATED,
            prompt='Create a compact family house.',
            prompt_message=message,
            job=job,
            status=ProcessingJob.COMPLETED,
        )

        response = self.client.post(
            reverse('projects:floor-plan-generate', args=[project.id]),
            {
                'prompt': 'Create a compact family house.',
                'retry_message_id': message.id,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('retry_message_id', response.data)
        self.assertEqual(
            ConversationMessage.objects.filter(
                conversation=conversation,
                role=ConversationMessage.USER,
            ).count(),
            1,
        )

    def test_project_with_a_three_d_version_can_be_deleted(self):
        project = self.create_project()
        upload_response = self.client.post(
            reverse('projects:floor-plan-upload', args=[project.id]),
            {
                'file': SimpleUploadedFile(
                    'floor.pdf',
                    b'pdf-content',
                    content_type='application/pdf',
                )
            },
            format='multipart',
        )
        self.assertEqual(upload_response.status_code, status.HTTP_201_CREATED)
        generate_response = self.client.post(
            reverse('projects:three-d-generate', args=[project.id]),
            {
                'prompt': 'Create a modern furnished render.',
                'floor_plan_version_id': upload_response.data['id'],
            },
            format='json',
        )
        self.assertEqual(generate_response.status_code, status.HTTP_202_ACCEPTED)

        delete_response = self.client.delete(
            reverse('projects:detail', args=[project.id])
        )

        self.assertEqual(delete_response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Project.objects.filter(id=project.id).exists())
        self.assertFalse(ThreeDVersion.objects.filter(project_id=project.id).exists())

        # Deleting the project must leave nothing on disk either: per-asset
        # deletion only unlinked tracked files and left the directory tree
        # (and anything untracked inside it) behind.
        project_directory = (
            Path(self.media_directory.name) / 'projects' / str(project.id)
        )
        self.assertFalse(project_directory.exists())
        self.assertFalse(ProjectAsset.objects.filter(project_id=project.id).exists())

    def test_deleting_a_block_removes_its_own_generated_image(self):
        """A later edit attaches the render it was edited from, which used to
        keep the generating block's own image alive on disk forever."""
        project = self.create_project()
        generate_response = self.client.post(
            reverse('projects:floor-plan-generate', args=[project.id]),
            {'prompt': 'Create a compact family house.'},
            format='json',
        )
        self.assertEqual(generate_response.status_code, status.HTTP_202_ACCEPTED)
        generated = FloorPlanVersion.objects.get(id=generate_response.data['id'])

        edit_response = self.client.post(
            reverse('projects:floor-plan-edit', args=[project.id]),
            {
                'original_version_id': generated.id,
                'instruction': 'Expand the living room.',
                'mask': SimpleUploadedFile('mask.png', b'mask', content_type='image/png'),
            },
            format='multipart',
        )
        self.assertEqual(edit_response.status_code, status.HTTP_202_ACCEPTED)
        edited = FloorPlanVersion.objects.get(id=edit_response.data['id'])

        # The edit's prompt message borrowed the generated plan's image.
        self.assertIn(
            generated.image_id,
            edited.prompt_message.attachments.values_list('id', flat=True),
        )

        generated_image_id = generated.image_id
        generated_image_name = generated.image.file.name
        storage = generated.image.file.storage

        delete_response = self.client.delete(
            reverse(
                'projects:message-delete',
                args=[project.id, generated.prompt_message_id],
            )
        )
        self.assertEqual(delete_response.status_code, status.HTTP_200_OK)

        # The block produced this image, so an attachment elsewhere must not
        # keep it: both the row and the stored file have to go.
        self.assertFalse(ProjectAsset.objects.filter(id=generated_image_id).exists())
        self.assertFalse(storage.exists(generated_image_name))

        # The edit's own image is untouched — only the targeted block went.
        self.assertTrue(ProjectAsset.objects.filter(id=edited.image_id).exists())

    def test_deleting_a_block_removes_the_assistant_reply_too(self):
        """The reply is a separate message row with no FK to the prompt, so a
        block delete that only removed the prompt left the answer on screen —
        for a BOQ turn that reply is the compiled table itself."""
        project = self.create_project()
        generate_response = self.client.post(
            reverse('projects:floor-plan-generate', args=[project.id]),
            {'prompt': 'Create a compact family house.'},
            format='json',
        )
        self.assertEqual(generate_response.status_code, status.HTTP_202_ACCEPTED)
        generated = FloorPlanVersion.objects.get(id=generate_response.data['id'])

        conversation = generated.prompt_message.conversation
        roles = list(conversation.messages.values_list('role', flat=True))
        self.assertEqual(
            roles,
            [ConversationMessage.USER, ConversationMessage.ASSISTANT],
        )

        delete_response = self.client.delete(
            reverse(
                'projects:message-delete',
                args=[project.id, generated.prompt_message_id],
            )
        )
        self.assertEqual(delete_response.status_code, status.HTTP_200_OK)

        # Prompt and reply both go: the whole exchange was one block.
        self.assertFalse(conversation.messages.exists())
        self.assertFalse(FloorPlanVersion.objects.filter(id=generated.id).exists())

    def test_deleting_a_three_d_block_removes_its_camera_snapshot(self):
        """The snapshot is reachable only through `job.parameters`, so it is on
        no FK and in no attachments and used to survive every block delete."""
        project = self.create_project()
        self.client.post(
            reverse('projects:floor-plan-upload', args=[project.id]),
            {
                'file': SimpleUploadedFile(
                    'floor.png', b'floor-plan-content', content_type='image/png'
                )
            },
            format='multipart',
        )
        three_d_response = self.client.post(
            reverse('projects:three-d-generate', args=[project.id]),
            {'prompt': 'Create a modern interior render.'},
            format='json',
        )
        self.assertEqual(three_d_response.status_code, status.HTTP_202_ACCEPTED)
        version = ThreeDVersion.objects.get(id=three_d_response.data['id'])

        snapshot_id = (version.job.parameters or {}).get('snapshot_asset_id')
        self.assertIsNotNone(snapshot_id)
        snapshot = ProjectAsset.objects.get(id=snapshot_id)
        snapshot_name = snapshot.file.name
        storage = snapshot.file.storage

        delete_response = self.client.delete(
            reverse(
                'projects:message-delete',
                args=[project.id, version.prompt_message_id],
            )
        )
        self.assertEqual(delete_response.status_code, status.HTTP_200_OK)

        self.assertFalse(ProjectAsset.objects.filter(id=snapshot_id).exists())
        self.assertFalse(storage.exists(snapshot_name))
        self.assertFalse(
            ProjectAsset.objects.filter(
                project=project, kind=ProjectAsset.THREE_D_SNAPSHOT
            ).exists()
        )

    def test_deleting_a_block_stops_at_the_next_prompt(self):
        project = self.create_project()
        first = self.client.post(
            reverse('projects:floor-plan-generate', args=[project.id]),
            {'prompt': 'Create a compact family house.'},
            format='json',
        )
        first_version = FloorPlanVersion.objects.get(id=first.data['id'])
        second = self.client.post(
            reverse('projects:floor-plan-edit', args=[project.id]),
            {
                'original_version_id': first_version.id,
                'instruction': 'Expand the living room.',
                'mask': SimpleUploadedFile('mask.png', b'mask', content_type='image/png'),
            },
            format='multipart',
        )
        second_version = FloorPlanVersion.objects.get(id=second.data['id'])

        self.client.delete(
            reverse(
                'projects:message-delete',
                args=[project.id, first_version.prompt_message_id],
            )
        )

        # The later turn is its own block and must be untouched.
        self.assertTrue(
            FloorPlanVersion.objects.filter(id=second_version.id).exists()
        )
        self.assertTrue(
            ConversationMessage.objects.filter(
                id=second_version.prompt_message_id
            ).exists()
        )

    def test_deleting_a_block_keeps_an_image_it_only_borrowed(self):
        project = self.create_project()
        generate_response = self.client.post(
            reverse('projects:floor-plan-generate', args=[project.id]),
            {'prompt': 'Create a compact family house.'},
            format='json',
        )
        generated = FloorPlanVersion.objects.get(id=generate_response.data['id'])
        edit_response = self.client.post(
            reverse('projects:floor-plan-edit', args=[project.id]),
            {
                'original_version_id': generated.id,
                'instruction': 'Expand the living room.',
                'mask': SimpleUploadedFile('mask.png', b'mask', content_type='image/png'),
            },
            format='multipart',
        )
        edited = FloorPlanVersion.objects.get(id=edit_response.data['id'])

        borrowed_id = generated.image_id
        borrowed_name = generated.image.file.name
        storage = generated.image.file.storage

        # Delete the edit block, which only borrowed the generated image.
        delete_response = self.client.delete(
            reverse(
                'projects:message-delete',
                args=[project.id, edited.prompt_message_id],
            )
        )
        self.assertEqual(delete_response.status_code, status.HTTP_200_OK)

        self.assertTrue(ProjectAsset.objects.filter(id=borrowed_id).exists())
        self.assertTrue(storage.exists(borrowed_name))
        self.assertTrue(FloorPlanVersion.objects.filter(id=generated.id).exists())

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


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
    AI_PIPELINE_ENABLED=True,
    AI_PLACEHOLDER_DELAY_SECONDS=0,
    CHANNEL_LAYERS=TEST_CHANNEL_LAYERS,
)
class AIPipelineContractTests(APITestCase):
    def setUp(self):
        self.media_directory = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media_directory.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(self.media_directory.cleanup)

        self.user = User.objects.create_user(
            email='pipeline@example.com',
            password='StrongPassword123!',
            full_name='Pipeline Architect',
            firm_name='Pipeline Firm',
            country='Pakistan',
            is_active=True,
        )
        self.client.force_authenticate(self.user)

    def create_step_two_project(self):
        project = Project.objects.create(
            owner=self.user,
            name='AI Pipeline Project',
            workflow=Project.STEP_2_ONLY,
        )
        upload_response = self.client.post(
            reverse('projects:step-two-input', args=[project.id]),
            {
                'file': SimpleUploadedFile(
                    'floor.png',
                    PLACEHOLDER_PNG,
                    content_type='image/png',
                )
            },
            format='multipart',
        )
        self.assertEqual(upload_response.status_code, status.HTTP_201_CREATED)
        floor_plan = FloorPlanVersion.objects.get(id=upload_response.data['id'])
        return project, floor_plan

    def guided_result(self):
        return SimpleNamespace(
            success=True,
            render_images=[PLACEHOLDER_PNG],
            render_paths=[],
            render_metadata={
                'provider': 'gemini',
                'guided_debug': {'snapshot_path': '/app/private/snapshot.png'},
            },
            warnings=[],
            error=None,
        )

    @patch('projects.ai_pipeline.execute_ai_job')
    def test_floor_plan_analysis_persists_geometry_on_existing_asset(self, execute):
        project, floor_plan = self.create_step_two_project()

        def analysis_result(job):
            return FloorPlanAnalysisOutput(
                asset=floor_plan.image,
                metadata={
                    'ai_pipeline': True,
                    'floorplan_json': '{"units":"m","walls":[]}',
                    'floorplan_data': {'units': 'm', 'walls': []},
                },
            )

        execute.side_effect = analysis_result
        response = self.client.post(
            reverse('projects:floor-plan-analyze', args=[project.id]),
            {'floor_plan_version_id': floor_plan.id},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        job = ProcessingJob.objects.get(id=response.data['id'])
        self.assertEqual(job.status, ProcessingJob.COMPLETED)
        self.assertEqual(job.output_asset_id, floor_plan.image_id)
        floor_plan.image.refresh_from_db()
        self.assertIn('floorplan_json', floor_plan.image.metadata)

    @patch('projects.ai_pipeline.execute_ai_job')
    def test_snapshot_contract_drives_real_three_d_result(self, execute):
        project, floor_plan = self.create_step_two_project()
        floor_plan.image.metadata = {'floorplan_json': FLOOR_PLAN_JSON}
        floor_plan.image.save(update_fields=['metadata'])

        snapshot_response = self.client.post(
            reverse('projects:three-d-snapshot-upload', args=[project.id]),
            {
                'file': SimpleUploadedFile(
                    'webgl-snapshot.png',
                    PLACEHOLDER_PNG,
                    content_type='image/png',
                ),
                'floor_plan_version_id': str(floor_plan.id),
                'rooms': json.dumps([{'name': 'Living Room'}]),
            },
            format='multipart',
        )
        self.assertEqual(snapshot_response.status_code, status.HTTP_201_CREATED)

        execute.return_value = ImagePipelineOutput(
            content=PLACEHOLDER_PNG,
            filename='guided-result.png',
            content_type='image/png',
            metadata={'ai_pipeline': True, 'source': 'guided_render'},
        )
        generate_response = self.client.post(
            reverse('projects:three-d-generate', args=[project.id]),
            {
                'prompt': 'Generate a faithful SketchUp render.',
                'floor_plan_version_id': str(floor_plan.id),
                'snapshot_asset_id': snapshot_response.data['id'],
                'render_style': ThreeDVersion.SKETCHUP,
            },
            format='json',
        )

        self.assertEqual(generate_response.status_code, status.HTTP_202_ACCEPTED)
        version = ThreeDVersion.objects.get(id=generate_response.data['id'])
        self.assertEqual(version.status, ProcessingJob.COMPLETED)
        self.assertEqual(version.image.metadata['source'], 'guided_render')
        self.assertFalse(version.image.metadata.get('placeholder', False))

    @patch('app.ai.guided_rendering.generate_guided_render_checked')
    def test_generation_without_a_webgl_snapshot_renders_one_server_side(
        self,
        guided,
    ):
        project, floor_plan = self.create_step_two_project()
        floor_plan.image.metadata = {'floorplan_json': FLOOR_PLAN_JSON}
        floor_plan.image.save(update_fields=['metadata'])
        guided.return_value = self.guided_result()

        response = self.client.post(
            reverse('projects:three-d-generate', args=[project.id]),
            {
                'prompt': 'Generate a faithful render.',
                'floor_plan_version_id': str(floor_plan.id),
                'render_style': ThreeDVersion.PHOTOREALISTIC,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        version = ThreeDVersion.objects.get(id=response.data['id'])
        self.assertEqual(version.status, ProcessingJob.COMPLETED)
        snapshot = project.assets.get(kind=ProjectAsset.THREE_D_SNAPSHOT)
        self.assertEqual(snapshot.metadata['source'], 'backend_massing_snapshot')
        # The client viewer captures on a 16:9 white frame with the grid
        # hidden; the fallback has to hand the model the same contract.
        with Image.open(snapshot.file.path) as rendered:
            self.assertEqual(rendered.size, (CANVAS_W, CANVAS_H))
            self.assertEqual(rendered.convert('RGB').getpixel((2, 2)), (255, 255, 255))
        self.assertEqual(
            snapshot.metadata['rooms'][0],
            {
                'name': 'Living Room',
                'nx': 0.0,
                'ny': 0.0,
                'width_m': 4.0,
                'depth_m': 3.0,
                'area_m2': 12.0,
            },
        )
        version.job.refresh_from_db()
        self.assertEqual(
            version.job.parameters['snapshot_asset_id'],
            str(snapshot.id),
        )
        call = guided.call_args.kwargs
        self.assertEqual(call['mode'], 'photoreal')
        self.assertEqual(call['rooms'], 'Living Room')
        self.assertIn('Generate a faithful render.', call['description'])
        self.assertIn('Single-room studio', call['description'])

    @patch('app.ai.guided_rendering.analyze_floorplan_verified_sync')
    @patch('app.ai.guided_rendering.generate_guided_render_checked')
    def test_generation_analyzes_an_unanalyzed_floor_plan(self, guided, analyze):
        from app.ai.floorplan_schema import FloorPlan

        project, floor_plan = self.create_step_two_project()
        guided.return_value = self.guided_result()
        analyze.return_value = (FloorPlan.model_validate_json(FLOOR_PLAN_JSON), None)

        response = self.client.post(
            reverse('projects:three-d-generate', args=[project.id]),
            {
                'prompt': 'Generate a faithful render.',
                'floor_plan_version_id': str(floor_plan.id),
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(analyze.call_count, 1)
        floor_plan.image.refresh_from_db()
        self.assertIn('floorplan_json', floor_plan.image.metadata)
        self.assertEqual(
            floor_plan.image.metadata['floorplan_data']['rooms'][0]['name'],
            'Living Room',
        )

    @patch('app.ai.guided_rendering.generate_guided_render_checked')
    def test_adapter_calls_official_guided_renderer_with_snapshot(self, guided):
        project, floor_plan = self.create_step_two_project()
        floor_plan.image.metadata = {'floorplan_json': FLOOR_PLAN_JSON}
        floor_plan.image.save(update_fields=['metadata'])
        snapshot = ProjectAsset.objects.create(
            project=project,
            uploaded_by=self.user,
            kind=ProjectAsset.THREE_D_SNAPSHOT,
            file=SimpleUploadedFile(
                'webgl.png',
                PLACEHOLDER_PNG,
                content_type='image/png',
            ),
            original_name='webgl.png',
            content_type='image/png',
            size=len(PLACEHOLDER_PNG),
            metadata={
                'source': 'frontend_three_webgl',
                'floor_plan_version_id': str(floor_plan.id),
                'rooms': [{'name': 'Living Room'}],
            },
        )
        conversation = Conversation.objects.create(
            project=project,
            kind=Conversation.THREE_D,
        )
        message = ConversationMessage.objects.create(
            conversation=conversation,
            role=ConversationMessage.USER,
            content='Create the guided render.',
        )
        job = ProcessingJob.objects.create(
            project=project,
            created_by=self.user,
            job_type=ProcessingJob.THREE_D_GENERATE,
            parameters={
                'snapshot_asset_id': str(snapshot.id),
                'style_reference_asset_ids': [],
            },
        )
        ThreeDVersion.objects.create(
            project=project,
            created_by=self.user,
            source=ThreeDVersion.GENERATED,
            render_style=ThreeDVersion.SKETCHUP,
            prompt_message=message,
            floor_plan=floor_plan,
            job=job,
        )
        guided.return_value = self.guided_result()

        output = generate_three_d(job)

        call = guided.call_args.kwargs
        self.assertEqual(call['project_id'], str(project.id))
        self.assertEqual(call['rooms'], 'Living Room')
        geometry = json.loads(call['floorplan_json'])
        stored_geometry = json.loads(floor_plan.image.metadata['floorplan_json'])
        self.assertEqual(geometry['rooms'], stored_geometry['rooms'])
        self.assertEqual(len(geometry['walls']), len(stored_geometry['walls']))
        self.assertEqual(output.metadata['source'], 'guided_render')
        self.assertNotIn(
            'snapshot_path',
            output.metadata['render_metadata']['guided_debug'],
        )

    @patch('projects.ai_pipeline.execute_ai_job')
    def test_pending_boq_agent_turn_stays_in_conversation_only(self, execute):
        project = Project.objects.create(
            owner=self.user,
            name='BOQ Agent Project',
            workflow=Project.STEP_3_ONLY,
        )
        execute.return_value = BOQPipelineOutput(
            response_text='Please approve Step 1 so I can extract the materials.',
            structured_data={
                'columns': [],
                'rows': [],
                'workflow_pending': True,
            },
        )

        response = self.client.post(
            reverse('projects:boq-generate', args=[project.id]),
            {'prompt': 'Create my BOQ.'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        job = ProcessingJob.objects.get(id=response.data['job']['id'])
        self.assertEqual(job.status, ProcessingJob.COMPLETED)
        self.assertFalse(BOQVersion.objects.filter(id=response.data['id']).exists())
        self.assertTrue(
            ConversationMessage.objects.filter(
                conversation__project=project,
                role=ConversationMessage.ASSISTANT,
                content__icontains='approve Step 1',
            ).exists()
        )

    def completed_three_d(self, project, floor_plan, render_style):
        image = ProjectAsset.objects.create(
            project=project,
            uploaded_by=self.user,
            kind=ProjectAsset.THREE_D_IMAGE,
            file=SimpleUploadedFile(
                'render.png',
                PLACEHOLDER_PNG,
                content_type='image/png',
            ),
            original_name='render.png',
            content_type='image/png',
            size=len(PLACEHOLDER_PNG),
        )
        return ThreeDVersion.objects.create(
            project=project,
            created_by=self.user,
            source=ThreeDVersion.GENERATED,
            render_style=render_style,
            floor_plan=floor_plan,
            image=image,
            status=ProcessingJob.COMPLETED,
        )

    @patch('app.ai_service.edit_3d_area')
    def test_masked_edit_composites_the_annotation_for_the_area_editor(self, edit):
        project, floor_plan = self.create_step_two_project()
        original = self.completed_three_d(
            project,
            floor_plan,
            ThreeDVersion.PHOTOREALISTIC,
        )
        edit.return_value = self.guided_result()

        response = self.client.post(
            reverse('projects:three-d-edit', args=[project.id]),
            {
                'original_version_id': str(original.id),
                'instruction': 'Replace the office with cinema seating.',
                'mask': SimpleUploadedFile(
                    'mask.png',
                    PLACEHOLDER_PNG,
                    content_type='image/png',
                ),
            },
            format='multipart',
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        version = ThreeDVersion.objects.get(id=response.data['id'])
        self.assertEqual(version.status, ProcessingJob.COMPLETED)
        self.assertEqual(version.image.metadata['source'], 'render_edit')
        call = edit.call_args.kwargs
        self.assertEqual(call['style'], 'photoreal')
        self.assertEqual(call['instruction'], 'Replace the office with cinema seating.')
        self.assertTrue(call['annotated_image_url'].endswith('annotated-render.png'))

    @patch('app.ai_service.edit_3d_chat')
    def test_text_only_revision_uses_the_live_chat_editor(self, edit):
        project, floor_plan = self.create_step_two_project()
        original = self.completed_three_d(project, floor_plan, ThreeDVersion.SKETCHUP)
        edit.return_value = self.guided_result()

        response = self.client.post(
            reverse('projects:three-d-generate', args=[project.id]),
            {
                'prompt': 'Warm up the lighting.',
                'original_version_id': str(original.id),
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        version = ThreeDVersion.objects.get(id=response.data['id'])
        self.assertEqual(version.source, ThreeDVersion.EDITED)
        self.assertEqual(version.status, ProcessingJob.COMPLETED)
        call = edit.call_args.kwargs
        self.assertEqual(call['instruction'], 'Warm up the lighting.')
        self.assertEqual(call['style'], 'sketchup')

    @patch('app.ai.rendering.service.generate_angled_view')
    def test_angle_endpoint_uses_the_isometric_generator(self, angled):
        project, floor_plan = self.create_step_two_project()
        original = self.completed_three_d(project, floor_plan, ThreeDVersion.SKETCHUP)
        render_path = Path(self.media_directory.name) / 'isometric.png'
        render_path.write_bytes(PLACEHOLDER_PNG)
        angled.return_value = SimpleNamespace(
            success=True,
            output_abs_path=str(render_path),
            warnings=[],
            metadata={'view_type': 'isometric'},
            error=None,
        )

        response = self.client.post(
            reverse('projects:three-d-angle', args=[project.id]),
            {'original_version_id': str(original.id)},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        version = ThreeDVersion.objects.get(id=response.data['id'])
        self.assertEqual(version.angle, ThreeDVersion.ISOMETRIC_45)
        self.assertEqual(version.status, ProcessingJob.COMPLETED)
        self.assertEqual(version.image.metadata['source'], 'isometric_angle')
        self.assertEqual(angled.call_args.kwargs['view_type'], 'isometric')

    @patch('app.ai.boq.run_boq_agent')
    def test_boq_turn_gives_the_agent_documents_and_room_geometry(self, agent):
        from app.ai.config import ai_settings

        agent.return_value = 'Please approve Step 1.'
        project = Project.objects.create(
            owner=self.user,
            name='BOQ Context Project',
            workflow=Project.STEP_3_ONLY,
        )
        floor_plan_asset = ProjectAsset.objects.create(
            project=project,
            uploaded_by=self.user,
            kind=ProjectAsset.FLOOR_PLAN,
            file=SimpleUploadedFile('plan.png', PLACEHOLDER_PNG, content_type='image/png'),
            original_name='plan.png',
            content_type='image/png',
            size=len(PLACEHOLDER_PNG),
            metadata={'floorplan_data': json.loads(FLOOR_PLAN_JSON)},
        )
        project.selected_floor_plan = FloorPlanVersion.objects.create(
            project=project,
            created_by=self.user,
            source=FloorPlanVersion.UPLOADED,
            image=floor_plan_asset,
            status=ProcessingJob.COMPLETED,
        )
        project.save(update_fields=['selected_floor_plan'])
        document_response = self.client.post(
            reverse('projects:document-list-create', args=[project.id]),
            {
                'file': SimpleUploadedFile(
                    'brief.pdf',
                    b'%PDF-1.4 brief',
                    content_type='application/pdf',
                ),
                'document_type': ProjectDocument.PROJECT_BRIEF,
            },
            format='multipart',
        )
        self.assertEqual(document_response.status_code, status.HTTP_201_CREATED)

        with tempfile.TemporaryDirectory() as output_directory:
            with patch.object(ai_settings, 'SCRATCH_DIR', output_directory):
                response = self.client.post(
                    reverse('projects:boq-generate', args=[project.id]),
                    {'prompt': 'Create my BOQ.'},
                    format='json',
                )
                self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
                staged = list(Path(output_directory, 'uploads').iterdir())

        self.assertEqual([path.name.endswith('-brief.pdf') for path in staged], [True])
        context = agent.call_args.kwargs['project_context']
        self.assertIn('Living Room: 12.00 m2', context)
        self.assertIn('Project brief', context)
        self.assertIn(staged[0].name, context)

    @patch('app.ai.boq.run_boq_agent')
    def test_boq_turn_extracts_an_mep_excel_schedule_into_the_context(self, agent):
        import openpyxl

        from app.ai.config import ai_settings

        agent.return_value = 'Please approve Step 1.'
        project = Project.objects.create(
            owner=self.user,
            name='MEP Schedule Project',
            workflow=Project.STEP_3_ONLY,
        )

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = 'Doors'
        sheet.append(['Mark', 'Type', 'Width', 'Count'])
        sheet.append(['D1', 'Flush door', 900, 12])
        buffer = io.BytesIO()
        workbook.save(buffer)

        document_response = self.client.post(
            reverse('projects:document-list-create', args=[project.id]),
            {
                'file': SimpleUploadedFile(
                    'door-schedule.xlsx',
                    buffer.getvalue(),
                    content_type=(
                        'application/vnd.openxmlformats-officedocument'
                        '.spreadsheetml.sheet'
                    ),
                ),
                'document_type': ProjectDocument.DOOR_WINDOW_SCHEDULE,
            },
            format='multipart',
        )
        self.assertEqual(document_response.status_code, status.HTTP_201_CREATED)

        with tempfile.TemporaryDirectory() as output_directory:
            with patch.object(ai_settings, 'SCRATCH_DIR', output_directory):
                response = self.client.post(
                    reverse('projects:boq-generate', args=[project.id]),
                    {'prompt': 'Create my BOQ.'},
                    format='json',
                )
                self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        context = agent.call_args.kwargs['project_context']
        self.assertIn('Door & Window Schedule', context)
        self.assertIn('D1 | Flush door | 900 | 12', context)

    @patch('projects.ai_pipeline.execute_ai_job')
    def test_final_boq_agent_table_is_kept_and_can_be_approved(self, execute):
        project = Project.objects.create(
            owner=self.user,
            name='Final BOQ Agent Project',
            workflow=Project.STEP_3_ONLY,
        )
        execute.return_value = BOQPipelineOutput(
            response_text='Final BOQ table is ready.',
            structured_data={
                'columns': ['Item', 'Description', 'Quantity', 'Unit', 'Rate', 'Amount'],
                'rows': [
                    {
                        'Item': '1',
                        'Description': 'Concrete',
                        'Quantity': '10',
                        'Unit': 'm3',
                        'Rate': '100',
                        'Amount': '1000',
                    }
                ],
                'workflow_pending': False,
            },
        )

        response = self.client.post(
            reverse('projects:boq-generate', args=[project.id]),
            {'prompt': 'Approved. Compile Step 4.'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        version = BOQVersion.objects.get(id=response.data['id'])
        self.assertEqual(version.status, ProcessingJob.COMPLETED)
        approve_response = self.client.post(
            reverse('projects:boq-approve', args=[project.id, version.id])
        )
        self.assertEqual(approve_response.status_code, status.HTTP_200_OK)
        self.assertEqual(str(project.id), str(approve_response.data['id']))
