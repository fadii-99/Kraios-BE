from django.urls import reverse
from django.conf import settings
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from .models import SignupRequest, User


class SignupRequestAPITests(APITestCase):
    def setUp(self):
        self.url = reverse('accounts:signup-request')
        self.payload = {
            'name': 'Ayesha Khan',
            'firm': 'Khan Architecture',
            'email': 'Ayesha@Example.com',
            'country': 'Pakistan',
            'date': '2026-09-15',
            'time': '10:00 AM - 11:00 AM',
        }

    def test_signup_creates_inactive_user_and_pending_request(self):
        response = self.client.post(self.url, self.payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], SignupRequest.PENDING)

        user = User.objects.get(email='ayesha@example.com')
        self.assertFalse(user.is_active)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.full_name, self.payload['name'])
        self.assertEqual(user.firm_name, self.payload['firm'])
        self.assertEqual(user.role, User.ARCHITECT_ACCOUNT)

        signup_request = SignupRequest.objects.get(user=user)
        self.assertEqual(str(signup_request.preferred_date), self.payload['date'])
        self.assertEqual(signup_request.preferred_time, self.payload['time'])

    def test_signup_rejects_duplicate_email(self):
        self.client.post(self.url, self.payload, format='json')
        duplicate_payload = self.payload.copy()
        duplicate_payload['email'] = 'AYESHA@example.com'

        response = self.client.post(self.url, duplicate_payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('email', response.data)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(SignupRequest.objects.count(), 1)

    def test_signup_requires_all_fields(self):
        response = self.client.post(self.url, {}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            set(response.data.keys()),
            {'name', 'firm', 'email', 'country', 'date', 'time'},
        )


class AuthenticationAPITests(APITestCase):
    def setUp(self):
        self.csrf_url = reverse('accounts:csrf')
        self.login_url = reverse('accounts:login')
        self.refresh_url = reverse('accounts:refresh')
        self.logout_url = reverse('accounts:logout')
        self.me_url = reverse('accounts:me')
        self.password = 'StrongInitialPassword123!'
        self.user = User.objects.create_user(
            email='user@example.com',
            password=self.password,
            full_name='Test Architect',
            firm_name='Test Firm',
            country='Pakistan',
            job_title='Senior Architect',
            phone='+92-300-0000000',
            is_active=True,
        )

    def test_active_user_can_login(self):
        response = self.client.post(
            self.login_url,
            {'email': self.user.email, 'password': self.password},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn('access', response.data)
        self.assertNotIn('refresh', response.data)
        self.assertIn(settings.AUTH_ACCESS_COOKIE_NAME, response.cookies)
        self.assertIn(settings.AUTH_REFRESH_COOKIE_NAME, response.cookies)
        self.assertTrue(response.cookies[settings.AUTH_ACCESS_COOKIE_NAME]['httponly'])
        self.assertTrue(response.cookies[settings.AUTH_REFRESH_COOKIE_NAME]['httponly'])
        self.assertEqual(response.data['user']['email'], self.user.email)
        self.assertEqual(response.data['user']['full_name'], self.user.full_name)

    def test_inactive_user_cannot_login(self):
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])

        response = self.client.post(
            self.login_url,
            {'email': self.user.email, 'password': self.password},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('not active', response.data['detail'])

    def test_inactive_user_with_wrong_password_gets_generic_error(self):
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])

        response = self.client.post(
            self.login_url,
            {'email': self.user.email, 'password': 'wrong-password'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data['detail'], 'Invalid email or password.')

    def test_invalid_password_is_rejected(self):
        response = self.client.post(
            self.login_url,
            {'email': self.user.email, 'password': 'wrong-password'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_user_can_get_profile(self):
        self.client.post(
            self.login_url,
            {'email': self.user.email, 'password': self.password},
            format='json',
        )

        response = self.client.get(self.me_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['email'], self.user.email)
        self.assertEqual(response.data['firm_name'], self.user.firm_name)
        self.assertEqual(response.data['role'], User.ARCHITECT_ACCOUNT)

    def test_profile_requires_authentication(self):
        response = self.client.get(self.me_url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_refresh_rotates_refresh_cookie(self):
        self.client.post(
            self.login_url,
            {'email': self.user.email, 'password': self.password},
            format='json',
        )
        original_refresh_token = self.client.cookies[
            settings.AUTH_REFRESH_COOKIE_NAME
        ].value

        response = self.client.post(self.refresh_url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertIn(settings.AUTH_ACCESS_COOKIE_NAME, response.cookies)
        self.assertIn(settings.AUTH_REFRESH_COOKIE_NAME, response.cookies)
        self.assertNotEqual(
            original_refresh_token,
            response.cookies[settings.AUTH_REFRESH_COOKIE_NAME].value,
        )

        with self.assertRaises(TokenError):
            RefreshToken(original_refresh_token)

    def test_logout_blacklists_refresh_token_and_clears_cookies(self):
        self.client.post(
            self.login_url,
            {'email': self.user.email, 'password': self.password},
            format='json',
        )
        refresh_token = self.client.cookies[settings.AUTH_REFRESH_COOKIE_NAME].value

        response = self.client.post(self.logout_url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(
            response.cookies[settings.AUTH_ACCESS_COOKIE_NAME]['max-age'],
            0,
        )
        self.assertEqual(
            response.cookies[settings.AUTH_REFRESH_COOKIE_NAME]['max-age'],
            0,
        )
        with self.assertRaises(TokenError):
            RefreshToken(refresh_token)

    def test_login_requires_csrf_token(self):
        csrf_client = self.client_class(enforce_csrf_checks=True)

        response = csrf_client.post(
            self.login_url,
            {'email': self.user.email, 'password': self.password},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        csrf_response = csrf_client.get(self.csrf_url)
        csrf_token = csrf_response.cookies['csrftoken'].value
        self.assertIn('csrfToken', csrf_response.data)
        response = csrf_client.post(
            self.login_url,
            {'email': self.user.email, 'password': self.password},
            format='json',
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class AdminPanelTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            email='admin@example.com',
            password='AdminPassword123!',
            full_name='Kraios Admin',
            firm_name='Kraios',
            country='Pakistan',
        )
        self.applicant = User.objects.create_user(
            email='applicant@example.com',
            password=None,
            full_name='Pending Applicant',
            firm_name='Applicant Firm',
            country='Pakistan',
        )
        self.signup_request = SignupRequest.objects.create(
            user=self.applicant,
            preferred_date='2026-09-20',
            preferred_time='2:00 PM - 3:00 PM',
        )
        self.client.force_login(self.admin_user)

    def test_pending_signup_request_appears_in_admin(self):
        url = reverse('admin:accounts_signuprequest_changelist')

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertContains(response, self.applicant.email)
        self.assertContains(response, SignupRequest.PENDING)

    def test_admin_can_open_user_password_change_page(self):
        url = reverse(
            'admin:auth_user_password_change',
            args=[self.applicant.id],
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
