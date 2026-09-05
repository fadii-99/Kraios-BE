"""HTTP and task tests for the BIM engine.

No model provider is contacted: the extraction pipeline is patched at
`bim.tasks.extract_plan`, which is the seam between "the app" and "the AI". The
pipeline's own behaviour is covered in `test_extraction.py`.
"""
from __future__ import annotations

import io
import tempfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from bim.ai.extractor import AttemptRecord, ExtractionError, ExtractionResult
from bim.grading import grade
from bim.models import BimExtraction, BimSource
from bim.normalize import apply_defaults
from bim.schema import BimPlan, BuildingType
from bim.ai.prompts import EXAMPLE_PLAN


def _png_bytes(width=1200, height=800, colour=(250, 250, 250)) -> bytes:
    buffer = io.BytesIO()
    image = Image.new('RGB', (width, height), colour)
    # A few dark pixels so the file is not a uniform block; irrelevant to the
    # tests but keeps the fixture honest about being a drawing.
    for x in range(0, width, 40):
        for y in range(0, height, 40):
            image.putpixel((x, y), (20, 20, 20))
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def _upload(name='plan.png') -> SimpleUploadedFile:
    return SimpleUploadedFile(name, _png_bytes(), content_type='image/png')


def _example_result() -> ExtractionResult:
    plan = BimPlan.model_validate(
        apply_defaults(EXAMPLE_PLAN, building_type=BuildingType.HOUSE)
    )
    repaired, report = grade(plan)
    return ExtractionResult(
        plan=repaired,
        report=report,
        survey={'building_type': 'house'},
        attempts=[AttemptRecord(attempt=1, score=report.score, grade=report.grade,
                                accepted=True)],
        image_width=1200,
        image_height=800,
        total_ms=1234,
    )


class BimApiTestCase(APITestCase):
    def setUp(self):
        self.media_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.media_directory.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.media_directory.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)

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

    def upload_source(self, **kwargs):
        response = self.client.post(
            reverse('bim:source-list-create'),
            {'file': _upload(), **kwargs},
            format='multipart',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response.data

    def start_extraction(self, source_id):
        """POST an extraction and let its on_commit dispatch actually fire.

        `services.enqueue_extraction` hands the task to Celery from
        `transaction.on_commit`, which never runs under `TestCase` because the
        surrounding transaction is rolled back rather than committed. Without
        this wrapper the endpoint returns 202 and nothing is ever queued, which
        is a property of the test harness and not of the code.
        """
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                reverse('bim:extraction-list-create', args=[source_id])
            )


class SourceUploadTests(BimApiTestCase):
    def test_uploading_a_plan_stores_it_and_reports_its_dimensions(self):
        data = self.upload_source(name='Ground floor')

        self.assertEqual(data['name'], 'Ground floor')
        self.assertEqual(data['source_kind'], 'image')
        self.assertEqual(data['image_width'], 1200)
        self.assertEqual(data['image_height'], 800)
        self.assertIsNone(data['latest_extraction'])
        self.assertTrue(BimSource.objects.filter(id=data['id']).exists())

    def test_the_filename_is_used_when_no_name_is_given(self):
        data = self.upload_source()
        self.assertEqual(data['name'], 'plan.png')

    def test_a_file_that_is_not_an_image_is_refused(self):
        response = self.client.post(
            reverse('bim:source-list-create'),
            {'file': SimpleUploadedFile('notes.txt', b'hello', content_type='text/plain')},
            format='multipart',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('file', response.data)

    def test_a_file_with_an_image_name_but_no_image_inside_is_refused(self):
        """The extension check is a filter; decoding is the actual gate."""
        response = self.client.post(
            reverse('bim:source-list-create'),
            {'file': SimpleUploadedFile('plan.png', b'not a png', content_type='image/png')},
            format='multipart',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(BimSource.objects.exists())

    def test_an_empty_file_is_refused(self):
        response = self.client.post(
            reverse('bim:source-list-create'),
            {'file': SimpleUploadedFile('plan.png', b'', content_type='image/png')},
            format='multipart',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_list_only_shows_the_callers_own_plans(self):
        self.upload_source(name='Mine')
        self.client.force_authenticate(self.other_user)
        self.upload_source(name='Theirs')

        self.client.force_authenticate(self.user)
        response = self.client.get(reverse('bim:source-list-create'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([row['name'] for row in response.data], ['Mine'])

    def test_anonymous_callers_are_rejected(self):
        self.client.force_authenticate(None)
        response = self.client.get(reverse('bim:source-list-create'))
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


class SourceAccessTests(BimApiTestCase):
    def test_another_users_plan_is_not_found(self):
        source = self.upload_source()
        self.client.force_authenticate(self.other_user)

        for url in (
            reverse('bim:source-detail', args=[source['id']]),
            reverse('bim:source-file', args=[source['id']]),
            reverse('bim:extraction-list-create', args=[source['id']]),
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    self.client.get(url).status_code, status.HTTP_404_NOT_FOUND
                )

    def test_another_user_cannot_start_an_extraction_on_it(self):
        source = self.upload_source()
        self.client.force_authenticate(self.other_user)

        response = self.client.post(
            reverse('bim:extraction-list-create', args=[source['id']])
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(BimExtraction.objects.exists())

    def test_the_owner_can_download_the_drawing(self):
        source = self.upload_source()
        response = self.client.get(reverse('bim:source-file', args=[source['id']]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(b''.join(response.streaming_content).startswith(b'\x89PNG'))

    def test_deleting_a_plan_removes_it_and_its_extractions(self):
        source = self.upload_source()
        BimExtraction.objects.create(source_id=source['id'], created_by=self.user)

        response = self.client.delete(reverse('bim:source-detail', args=[source['id']]))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(BimSource.objects.exists())
        self.assertFalse(BimExtraction.objects.exists())


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class ExtractionFlowTests(BimApiTestCase):
    def test_starting_an_extraction_runs_it_and_stores_the_plan(self):
        source = self.upload_source()

        with patch('bim.tasks.extract_plan', return_value=_example_result()) as pipeline:
            response = self.start_extraction(source['id'])

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        pipeline.assert_called_once()

        extraction = BimExtraction.objects.get(id=response.data['id'])
        self.assertEqual(extraction.status, BimExtraction.COMPLETED)
        self.assertEqual(extraction.progress, 100)
        self.assertEqual(extraction.grade, 'A')
        self.assertEqual(extraction.score, 100)
        self.assertEqual(len(extraction.plan['walls']), 5)
        self.assertEqual(extraction.quality['grade'], 'A')
        self.assertIn('models', extraction.record)

    def test_the_detail_endpoint_returns_the_whole_plan(self):
        source = self.upload_source()
        with patch('bim.tasks.extract_plan', return_value=_example_result()):
            created = self.start_extraction(source['id'])

        response = self.client.get(
            reverse('bim:extraction-detail', args=[created.data['id']])
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], BimExtraction.COMPLETED)
        self.assertEqual(len(response.data['plan']['rooms']), 2)
        self.assertEqual(response.data['quality']['score'], 100)
        self.assertEqual(response.data['error'], '')

    def test_the_list_endpoint_omits_the_plan(self):
        source = self.upload_source()
        with patch('bim.tasks.extract_plan', return_value=_example_result()):
            self.start_extraction(source['id'])

        response = self.client.get(
            reverse('bim:extraction-list-create', args=[source['id']])
        )

        self.assertEqual(len(response.data), 1)
        self.assertNotIn('plan', response.data[0])
        self.assertEqual(response.data[0]['grade'], 'A')

    def test_a_failed_extraction_never_returns_the_internal_error(self):
        source = self.upload_source()
        secret = 'OPENROUTER_API_KEY=sk-live-should-never-be-shown'

        with patch('bim.tasks.extract_plan', side_effect=ExtractionError(secret)):
            created = self.start_extraction(source['id'])

        extraction = BimExtraction.objects.get(id=created.data['id'])
        self.assertEqual(extraction.status, BimExtraction.FAILED)
        self.assertIn(secret, extraction.error)  # kept internally

        response = self.client.get(
            reverse('bim:extraction-detail', args=[created.data['id']])
        )
        body = str(response.data)
        self.assertNotIn(secret, body)
        self.assertNotIn('sk-live', body)
        self.assertIn('could not be converted', response.data['error'])

    def test_a_second_extraction_is_refused_while_one_is_live(self):
        source = self.upload_source()
        # A row left PROCESSING is exactly the state the partial unique index
        # is there to protect, so the test creates one rather than racing.
        BimExtraction.objects.create(
            source_id=source['id'],
            created_by=self.user,
            status=BimExtraction.PROCESSING,
        )

        response = self.start_extraction(source['id'])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(BimExtraction.objects.count(), 1)

    def test_a_new_extraction_is_allowed_once_the_previous_one_finished(self):
        source = self.upload_source()
        BimExtraction.objects.create(
            source_id=source['id'],
            created_by=self.user,
            status=BimExtraction.COMPLETED,
        )

        with patch('bim.tasks.extract_plan', return_value=_example_result()):
            response = self.start_extraction(source['id'])

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(BimExtraction.objects.count(), 2)

    def test_the_source_list_carries_the_latest_extraction(self):
        source = self.upload_source()
        with patch('bim.tasks.extract_plan', return_value=_example_result()):
            self.start_extraction(source['id'])

        response = self.client.get(reverse('bim:source-list-create'))

        latest = response.data[0]['latest_extraction']
        self.assertIsNotNone(latest)
        self.assertEqual(latest['status'], BimExtraction.COMPLETED)
        self.assertEqual(latest['grade'], 'A')


class TaskIdempotencyTests(BimApiTestCase):
    """A redelivered Celery message must not run the pipeline a second time."""

    def test_a_redelivered_task_does_no_work(self):
        from bim.tasks import run_bim_extraction

        source = BimSource.objects.create(
            owner=self.user,
            name='plan',
            original_name='plan.png',
            size=len(_png_bytes()),
        )
        source.file.save('plan.png', SimpleUploadedFile('plan.png', _png_bytes()), save=True)
        extraction = BimExtraction.objects.create(source=source, created_by=self.user)

        with patch('bim.tasks.extract_plan', return_value=_example_result()) as pipeline:
            first = run_bim_extraction(str(extraction.id))
            second = run_bim_extraction(str(extraction.id))

        self.assertEqual(first, 'completed')
        self.assertEqual(second, 'already-claimed')
        self.assertEqual(pipeline.call_count, 1)

    def test_a_task_for_a_deleted_extraction_exits_quietly(self):
        from bim.tasks import run_bim_extraction

        self.assertEqual(
            run_bim_extraction('00000000-0000-0000-0000-000000000000'), 'missing'
        )
