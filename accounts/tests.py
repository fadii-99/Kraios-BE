import re
from datetime import date as date_type
from datetime import datetime, time, timedelta
from datetime import timezone as datetime_timezone

from django.core import mail
from django.urls import reverse
from django.conf import settings
from django.test import TestCase
from django.utils import timezone
from django.utils.formats import date_format
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from admin.models import AvailabilityBlackout, Meeting
from admin.services_meetings import open_days

from .emails import (
    FORGOT_PASSWORD_OTP_TEMPLATE_ID,
    SIGNUP_MEETING_CONFIRMATION_TEMPLATE_ID,
)
from .models import SignupRequest, User


def _next_weekday(start, weekday):
    """The first date on or after ``start`` that falls on ``weekday``."""
    return start + timedelta(days=(weekday - start.weekday()) % 7)


def _next_open_day():
    """
    The soonest date the seeded schedule actually offers, from tomorrow on.

    Asked of the schedule rather than assumed, so these tests keep booking a
    real slot if the seeded week ever changes - and fail loudly, on this line,
    if it is ever emptied.
    """
    start = timezone.now().date() + timedelta(days=1)
    days = open_days(start, start + timedelta(days=14))
    assert days, 'No bookable date in the next fortnight - is the week seeded?'
    return days[0]


class SignupRequestAPITests(APITestCase):
    def setUp(self):
        self.url = reverse('accounts:signup-request')
        # A real slot out of the seeded Mon-Fri 09:00-17:00 week, computed
        # rather than written down. A fixed date would book nothing the moment
        # it fell into the past, and a date reached by plain arithmetic lands on
        # a closed weekend two runs in seven.
        self.booking_date = _next_open_day()
        self.payload = {
            'name': 'Ayesha Khan',
            'firm': 'Khan Architecture',
            'email': 'Ayesha@Example.com',
            'country': 'Pakistan',
            'date': self.booking_date.isoformat(),
            'time': '10:00 AM - 11:00 AM',
        }

    def test_signup_creates_inactive_user_and_pending_request(self):
        response = self.client.post(self.url, self.payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], SignupRequest.PENDING)
        self.assertTrue(response.data['confirmation_email_sent'])

        user = User.objects.get(email='ayesha@example.com')
        self.assertFalse(user.is_active)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.full_name, self.payload['name'])
        self.assertEqual(user.firm_name, self.payload['firm'])
        self.assertEqual(user.role, User.ARCHITECT_ACCOUNT)

        signup_request = SignupRequest.objects.get(user=user)
        self.assertEqual(str(signup_request.preferred_date), self.payload['date'])
        self.assertEqual(signup_request.preferred_time, self.payload['time'])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            mail.outbox[0].extra_headers['X-Kraios-Template-ID'],
            SIGNUP_MEETING_CONFIRMATION_TEMPLATE_ID,
        )
        self.assertIn(date_format(self.booking_date, 'F j, Y'), mail.outbox[0].body)
        self.assertIn(self.payload['time'], mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, ['ayesha@example.com'])

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


class PublicBookingCalendarTests(APITestCase):
    """
    What the signup form is allowed to offer, and what it is allowed to book.

    The point of these is the JOIN: the two read endpoints and the signup
    endpoint have to agree, because a date the calendar offers and the
    serializer then refuses is a visitor filling in a form that cannot succeed.
    Each test therefore closes something in the admin's schedule and asserts on
    both sides of it.
    """

    def setUp(self):
        self.days_url = reverse('accounts:booking-days')
        self.slots_url = reverse('accounts:booking-slots')
        self.signup_url = reverse('accounts:signup-request')
        self.open_day = _next_open_day()

    def _signup(self, **overrides):
        payload = {
            'name': 'Ayesha Khan',
            'firm': 'Khan Architecture',
            'email': 'ayesha@example.com',
            'country': 'Pakistan',
            'date': self.open_day.isoformat(),
            'time': '10:00 AM',
        }
        payload.update(overrides)
        return self.client.post(self.signup_url, payload, format='json')

    def test_days_lists_open_dates_and_omits_the_closed_ones(self):
        response = self.client.get(
            self.days_url, {'month': self.open_day.strftime('%Y-%m')}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        days = response.data['days']

        self.assertIn(self.open_day.isoformat(), days)
        # The seeded week is Monday-Friday, so nothing in the answer is a
        # weekend - asserted over the whole list rather than on one date,
        # because one closed date could be a blackout instead of the rule.
        for day in days:
            self.assertLess(date_type.fromisoformat(day).weekday(), 5)

    def test_days_never_offers_yesterday(self):
        today = timezone.now().date()

        response = self.client.get(self.days_url, {'month': today.strftime('%Y-%m')})

        self.assertEqual(response.data['min_date'], today.isoformat())
        for day in response.data['days']:
            self.assertGreaterEqual(date_type.fromisoformat(day), today)

    def test_a_blacked_out_date_leaves_the_calendar_and_the_form_together(self):
        AvailabilityBlackout.objects.create(date=self.open_day, reason='Offsite')

        days = self.client.get(
            self.days_url, {'month': self.open_day.strftime('%Y-%m')}
        )
        slots = self.client.get(self.slots_url, {'date': self.open_day.isoformat()})
        booking = self._signup()

        self.assertNotIn(self.open_day.isoformat(), days.data['days'])
        self.assertEqual(slots.data['slots'], [])
        self.assertEqual(booking.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('time', booking.data)
        self.assertEqual(User.objects.count(), 0)

    def test_slots_carry_the_label_the_form_submits_back(self):
        response = self.client.get(self.slots_url, {'date': self.open_day.isoformat()})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        by_time = {slot['time']: slot for slot in response.data['slots']}

        self.assertEqual(by_time['09:00']['label'], '09:00 AM')
        self.assertEqual(by_time['14:30']['label'], '02:30 PM')
        self.assertTrue(by_time['14:30']['available'])

        # The label is the round trip: what the endpoint shows is what the form
        # sends, and the serializer has to accept it back.
        self.assertEqual(
            self._signup(time=by_time['14:30']['label']).status_code,
            status.HTTP_201_CREATED,
        )

    def test_a_taken_slot_is_shown_as_taken_and_cannot_be_booked_twice(self):
        holder = User.objects.create_user(
            email='holder@example.com',
            password=None,
            full_name='Holder',
            is_active=False,
        )
        Meeting.objects.create(
            user=holder,
            scheduled_at=datetime.combine(
                self.open_day, time(10, 0), tzinfo=datetime_timezone.utc
            ),
            status=Meeting.SCHEDULED,
        )

        slots = self.client.get(self.slots_url, {'date': self.open_day.isoformat()})
        by_time = {slot['time']: slot for slot in slots.data['slots']}

        # Reported as taken rather than dropped - the form strikes it through,
        # so "10:00 is gone" does not read as "the day starts at 10:30".
        self.assertIn('10:00', by_time)
        self.assertFalse(by_time['10:00']['available'])

        self.assertEqual(self._signup().status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Meeting.objects.count(), 1)

    def test_a_closed_weekday_cannot_be_booked(self):
        saturday = _next_weekday(self.open_day, weekday=5)

        response = self._signup(date=saturday.isoformat())

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('time', response.data)
        self.assertEqual(SignupRequest.objects.count(), 0)

    def test_a_time_between_slots_cannot_be_booked(self):
        # 09:07 sits inside the open window but on no half-hour boundary. The
        # window is not the offer; the grid the window generates is.
        response = self._signup(time='09:07 AM')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(User.objects.count(), 0)

    def test_a_date_in_the_past_cannot_be_booked(self):
        yesterday = timezone.now().date() - timedelta(days=1)

        response = self._signup(date=yesterday.isoformat())

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(User.objects.count(), 0)

    def test_the_booking_endpoints_need_no_session(self):
        # Public by necessity: the visitor has no account yet. Asserted so a
        # later default-permission change cannot quietly close the signup form.
        self.assertEqual(
            self.client.get(self.days_url).status_code, status.HTTP_200_OK
        )
        self.assertEqual(
            self.client.get(
                self.slots_url, {'date': self.open_day.isoformat()}
            ).status_code,
            status.HTTP_200_OK,
        )

    def test_a_malformed_month_is_refused_rather_than_guessed(self):
        response = self.client.get(self.days_url, {'month': 'next-tuesday'})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ForgotPasswordAPITests(APITestCase):
    def setUp(self):
        self.request_url = reverse('accounts:forgot-password-request')
        self.confirm_url = reverse('accounts:forgot-password-confirm')
        self.old_password = 'StrongInitialPassword123!'
        self.new_password = 'EvenStrongerPassword456!'
        self.user = User.objects.create_user(
            email='forgot@example.com',
            password=self.old_password,
            full_name='Forgot Password User',
            firm_name='Test Firm',
            country='Pakistan',
            is_active=True,
        )

    def otp_from_latest_email(self):
        match = re.search(r'\b(\d{6})\b', mail.outbox[-1].body)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_user_can_request_and_confirm_password_reset(self):
        request_response = self.client.post(
            self.request_url,
            {'email': 'FORGOT@example.com'},
            format='json',
        )

        self.assertEqual(request_response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            mail.outbox[0].extra_headers['X-Kraios-Template-ID'],
            FORGOT_PASSWORD_OTP_TEMPLATE_ID,
        )
        self.assertTrue(mail.outbox[0].alternatives)

        confirm_response = self.client.post(
            self.confirm_url,
            {
                'email': self.user.email,
                'verification_id': request_response.data['verification_id'],
                'otp': self.otp_from_latest_email(),
                'new_password': self.new_password,
            },
            format='json',
        )

        self.assertEqual(confirm_response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.new_password))
        self.assertFalse(self.user.check_password(self.old_password))

    def test_unknown_email_returns_generic_success_without_email(self):
        response = self.client.post(
            self.request_url,
            {'email': 'missing@example.com'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertIn('verification_id', response.data)
        self.assertEqual(len(mail.outbox), 0)

    def test_password_reset_rejects_wrong_otp(self):
        request_response = self.client.post(
            self.request_url,
            {'email': self.user.email},
            format='json',
        )
        actual_otp = self.otp_from_latest_email()
        wrong_otp = '000000' if actual_otp != '000000' else '000001'

        response = self.client.post(
            self.confirm_url,
            {
                'email': self.user.email,
                'verification_id': request_response.data['verification_id'],
                'otp': wrong_otp,
                'new_password': self.new_password,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.old_password))


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
