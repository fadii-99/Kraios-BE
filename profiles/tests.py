import re

from django.core import mail
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.emails import DELETE_ACCOUNT_OTP_TEMPLATE_ID
from .models import AccountVerification


class ProfileAPITests(APITestCase):
    def setUp(self):
        self.password = 'StrongInitialPassword123!'
        self.user = User.objects.create_user(
            email='profile@example.com',
            password=self.password,
            full_name='Profile User',
            firm_name='Original Firm',
            country='Pakistan',
            is_active=True,
        )
        self.client.force_authenticate(self.user)

    def otp_from_latest_email(self):
        match = re.search(r'\b(\d{6})\b', mail.outbox[-1].body)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_user_can_view_and_edit_profile_but_not_email(self):
        response = self.client.patch(
            reverse('profiles:detail'),
            {
                'full_name': 'Updated Name',
                'job_title': 'Principal Architect',
                'email': 'changed@example.com',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.full_name, 'Updated Name')
        self.assertEqual(self.user.job_title, 'Principal Architect')
        self.assertEqual(self.user.email, 'profile@example.com')

    def test_password_change_requires_current_password_and_otp(self):
        new_password = 'EvenStrongerPassword456!'
        request_response = self.client.post(
            reverse('profiles:password-change-request'),
            {
                'current_password': self.password,
                'new_password': new_password,
            },
            format='json',
        )

        self.assertEqual(request_response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(len(mail.outbox), 1)
        otp = self.otp_from_latest_email()

        confirm_response = self.client.post(
            reverse('profiles:password-change-confirm'),
            {
                'verification_id': request_response.data['verification_id'],
                'otp': otp,
            },
            format='json',
        )

        self.assertEqual(confirm_response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new_password))

    def test_delete_account_requires_password_and_otp(self):
        request_response = self.client.post(
            reverse('profiles:delete-account-request'),
            {'current_password': self.password},
            format='json',
        )
        self.assertEqual(
            mail.outbox[-1].extra_headers['X-Kraios-Template-ID'],
            DELETE_ACCOUNT_OTP_TEMPLATE_ID,
        )
        self.assertTrue(mail.outbox[-1].alternatives)
        otp = self.otp_from_latest_email()

        confirm_response = self.client.post(
            reverse('profiles:delete-account-confirm'),
            {
                'verification_id': request_response.data['verification_id'],
                'otp': otp,
            },
            format='json',
        )

        self.assertEqual(confirm_response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(User.objects.filter(email='profile@example.com').exists())

    def test_incorrect_otp_attempt_is_recorded(self):
        request_response = self.client.post(
            reverse('profiles:delete-account-request'),
            {'current_password': self.password},
            format='json',
        )
        actual_otp = self.otp_from_latest_email()
        incorrect_otp = '000000' if actual_otp != '000000' else '000001'
        response = self.client.post(
            reverse('profiles:delete-account-confirm'),
            {
                'verification_id': request_response.data['verification_id'],
                'otp': incorrect_otp,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        verification = AccountVerification.objects.get(
            id=request_response.data['verification_id']
        )
        self.assertEqual(verification.failed_attempts, 1)
