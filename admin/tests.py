"""
Tests for the admin console.

Weighted towards the things that would be dangerous to get wrong rather than
towards coverage: who can reach an admin endpoint, what happens to a session
when privilege is withdrawn, that a generated password never appears in a
response, and that a reminder is sent exactly once no matter how many workers
run the task.

Every test that touches plans, subscriptions or support points
``KRAIOS_ADMIN_DUMMY_STORE_PATH`` at a temporary file, so a test run cannot
read or overwrite the real placeholder store.
"""
import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from accounts.models import SignupRequest, User

from . import dummy_data
from .models import AdminAuditLog, AdminProfile, Meeting
from .services_auth import MAX_FAILURES_PER_EMAIL_IP
from .services_meetings import parse_slot_label
from .tasks import send_due_meeting_reminders
from .tokens import issue_admin_tokens


ADMIN_PASSWORD = 'AdminConsole!Passw0rd'
CUSTOMER_PASSWORD = 'CustomerPassw0rd!42'


def _next_weekday(start, weekday):
    """The first date on or after ``start`` that falls on ``weekday``."""
    offset = (weekday - start.weekday()) % 7
    return start + timedelta(days=offset)


def _at(day, hour, minute):
    """A UTC instant on ``day``, matching how meetings are stored."""
    from datetime import datetime
    from datetime import timezone as datetime_timezone

    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=datetime_timezone.utc)


class AdminTestCase(APITestCase):
    """Shared fixtures, plus an isolated placeholder store per test class."""

    @classmethod
    def setUpClass(cls):
        cls._store_directory = tempfile.mkdtemp(prefix='kraios-admin-tests-')
        cls._store_override = override_settings(
            KRAIOS_ADMIN_DUMMY_STORE_PATH=Path(cls._store_directory) / 'store.json'
        )
        cls._store_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._store_override.disable()
        shutil.rmtree(cls._store_directory, ignore_errors=True)

    def setUp(self):
        # The login throttle and the lockout both read shared state; a test
        # must not inherit another test's attempts.
        cache.clear()
        dummy_data.reset_store()

        self.admin_user = User.objects.create_user(
            email='ops@kraios.test',
            password=ADMIN_PASSWORD,
            full_name='Ops Admin',
            firm_name='KRAIOS',
            country='',
            is_active=True,
        )
        self.admin_profile = AdminProfile.objects.create(
            user=self.admin_user,
            role=AdminProfile.SUPER_ADMIN,
        )

        self.customer = User.objects.create_user(
            email='architect@example.test',
            password=CUSTOMER_PASSWORD,
            full_name='Amelia Hartley',
            firm_name='Hartley Architects',
            country='United Kingdom',
            is_active=True,
        )

    def sign_in(self, client=None):
        client = client or self.client
        response = client.post(
            reverse('kraios_admin:login'),
            {'email': self.admin_user.email, 'password': ADMIN_PASSWORD},
            format='json',
        )
        assert response.status_code == status.HTTP_200_OK, response.data
        return response

    def authenticate(self):
        """Put a valid admin session on ``self.client``."""
        access_token, refresh_token = issue_admin_tokens(self.admin_profile)
        self.client.cookies[settings.ADMIN_AUTH_ACCESS_COOKIE_NAME] = access_token
        self.client.cookies[settings.ADMIN_AUTH_REFRESH_COOKIE_NAME] = refresh_token


class AdminAuthenticationTests(AdminTestCase):
    def test_admin_can_sign_in_and_receives_session_cookies(self):
        response = self.sign_in()

        self.assertEqual(response.data['admin']['email'], self.admin_user.email)
        self.assertEqual(response.data['admin']['role'], 'Super Admin')
        self.assertIn(settings.ADMIN_AUTH_ACCESS_COOKIE_NAME, response.cookies)
        self.assertIn(settings.ADMIN_AUTH_REFRESH_COOKIE_NAME, response.cookies)
        self.assertTrue(response.cookies[settings.ADMIN_AUTH_ACCESS_COOKIE_NAME]['httponly'])

    def test_customer_credentials_are_refused_at_the_console(self):
        response = self.client.post(
            reverse('kraios_admin:login'),
            {'email': self.customer.email, 'password': CUSTOMER_PASSWORD},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        # The same message a wrong password gets: the console must not confirm
        # that an address exists as a customer.
        self.assertEqual(response.data['detail'], 'Incorrect email or password.')

    def test_repeated_failures_lock_the_caller_out(self):
        url = reverse('kraios_admin:login')
        payload = {'email': self.admin_user.email, 'password': 'wrong-password'}

        for _ in range(MAX_FAILURES_PER_EMAIL_IP):
            self.assertEqual(
                self.client.post(url, payload, format='json').status_code,
                status.HTTP_401_UNAUTHORIZED,
            )

        locked = self.client.post(url, payload, format='json')
        self.assertEqual(locked.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn('Retry-After', locked)

        # And the lockout applies even to the correct password, which is what
        # makes it a lockout rather than a hint.
        correct = self.client.post(
            url,
            {'email': self.admin_user.email, 'password': ADMIN_PASSWORD},
            format='json',
        )
        self.assertEqual(correct.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_admin_endpoint_rejects_an_anonymous_caller(self):
        response = self.client.get(reverse('kraios_admin:user-list'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_a_customer_token_cannot_reach_an_admin_endpoint(self):
        """
        A customer session presented in the admin cookie is refused.

        It carries no admin scope claim, and the customer login endpoint cannot
        mint one - so this is the whole class of "replay a customer session at
        the console" in a single check.
        """
        from rest_framework_simplejwt.tokens import RefreshToken

        customer_token = RefreshToken.for_user(self.customer)
        self.client.cookies[settings.ADMIN_AUTH_ACCESS_COOKIE_NAME] = str(
            customer_token.access_token
        )

        response = self.client.get(reverse('kraios_admin:user-list'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_deactivating_an_admin_kills_its_live_access_token(self):
        self.authenticate()
        self.assertEqual(
            self.client.get(reverse('kraios_admin:me')).status_code,
            status.HTTP_200_OK,
        )

        self.admin_profile.is_active = False
        self.admin_profile.save(update_fields=['is_active'])

        self.assertEqual(
            self.client.get(reverse('kraios_admin:me')).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_a_token_taken_before_sign_out_stops_working(self):
        """
        Signing out rotates the session secret, so a copy of the access token
        somebody took beforehand is dead too - not merely absent from the
        browser that gave it up.
        """
        stolen_token, _refresh = issue_admin_tokens(self.admin_profile)

        stale = APIClient()
        stale.cookies[settings.ADMIN_AUTH_ACCESS_COOKIE_NAME] = stolen_token
        self.assertEqual(
            stale.get(reverse('kraios_admin:me')).status_code,
            status.HTTP_200_OK,
        )

        self.sign_in()
        self.assertEqual(
            self.client.post(reverse('kraios_admin:logout')).status_code,
            status.HTTP_204_NO_CONTENT,
        )

        self.assertEqual(
            stale.get(reverse('kraios_admin:me')).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_writes_require_a_csrf_token(self):
        csrf_client = APIClient(enforce_csrf_checks=True)
        response = csrf_client.post(
            reverse('kraios_admin:login'),
            {'email': self.admin_user.email, 'password': ADMIN_PASSWORD},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_audit_log_is_super_admin_only(self):
        self.authenticate()
        self.assertEqual(
            self.client.get(reverse('kraios_admin:audit-logs')).status_code,
            status.HTTP_200_OK,
        )

        self.admin_profile.role = AdminProfile.SUPPORT_ADMIN
        self.admin_profile.save(update_fields=['role'])

        self.assertEqual(
            self.client.get(reverse('kraios_admin:audit-logs')).status_code,
            status.HTTP_403_FORBIDDEN,
        )


class AdminUserManagementTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_user_list_excludes_administrators(self):
        response = self.client.get(reverse('kraios_admin:user-list'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        emails = {row['email'] for row in response.data['items']}
        self.assertIn(self.customer.email, emails)
        self.assertNotIn(self.admin_user.email, emails)

    def test_an_administrator_cannot_be_opened_as_a_customer(self):
        response = self.client.get(
            reverse('kraios_admin:user-detail', args=[self.admin_user.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_generating_a_password_activates_the_account_and_emails_it(self):
        pending = User.objects.create_user(
            email='pending@example.test',
            password=None,
            full_name='Pending Person',
            firm_name='Pending Firm',
            country='Ireland',
            is_active=False,
        )

        response = self.client.post(
            reverse('kraios_admin:user-generate-password', args=[pending.pk])
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['credentialsEmailSent'])

        pending.refresh_from_db()
        self.assertTrue(pending.is_active)
        self.assertTrue(pending.has_usable_password())

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [pending.email])

        # The password is in the email and nowhere else.
        body = mail.outbox[0].body
        serialised = str(response.data)
        for line in body.splitlines():
            if line.startswith('Password:'):
                secret = line.split('Password:', 1)[1].strip()
                break
        else:
            self.fail('The credentials email did not carry a password.')

        self.assertTrue(pending.check_password(secret))
        self.assertNotIn(secret, serialised)

    def test_deactivating_a_user_records_an_audit_entry(self):
        response = self.client.post(
            reverse('kraios_admin:user-status', args=[self.customer.pk]),
            {'status': 'Inactive'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['accountStatus'], 'Inactive')

        self.customer.refresh_from_db()
        self.assertFalse(self.customer.is_active)

        self.assertTrue(
            AdminAuditLog.objects.filter(
                action='user.deactivate',
                target_id=str(self.customer.pk),
            ).exists()
        )

    def test_editing_to_a_taken_email_is_refused(self):
        User.objects.create_user(
            email='taken@example.test',
            password=CUSTOMER_PASSWORD,
            full_name='Taken',
            firm_name='Taken Firm',
            country='Spain',
            is_active=True,
        )

        response = self.client.patch(
            reverse('kraios_admin:user-detail', args=[self.customer.pk]),
            {'email': 'taken@example.test'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.email, 'architect@example.test')

    def test_assigning_a_plan_sets_a_thirty_day_period_by_default(self):
        response = self.client.post(
            reverse('kraios_admin:user-subscription', args=[self.customer.pk]),
            {'planId': 'plan-studio', 'billingCycle': 'Monthly'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        subscription = response.data['subscriptionDetail']
        self.assertEqual(subscription['plan'], 'Studio')
        self.assertEqual(subscription['durationDays'], 30)
        self.assertEqual(subscription['status'], 'Active')

    def test_the_billing_cycle_is_also_the_term(self):
        # One choice decides both the price and the end date. Annual is 365
        # days at the annual rate; there is no separate term to disagree with
        # it, which is what used to put "PS1,928/yr" on an account whose access
        # ended in a month.
        annual = self.client.post(
            reverse('kraios_admin:user-subscription', args=[self.customer.pk]),
            {'planId': 'plan-studio', 'billingCycle': 'Annual'},
            format='json',
        )

        detail = annual.data['subscriptionDetail']
        self.assertEqual(detail['durationDays'], 365)
        self.assertEqual(
            date.fromisoformat(detail['renewalDate'])
            - date.fromisoformat(detail['startDate']),
            timedelta(days=365),
        )

    def test_a_term_sent_by_a_stale_client_is_ignored_not_obeyed(self):
        # The field is gone from the contract. A caller that still sends it
        # must not be able to grant a decade — it is simply not read.
        response = self.client.post(
            reverse('kraios_admin:user-subscription', args=[self.customer.pk]),
            {'planId': 'plan-studio', 'billingCycle': 'Monthly', 'durationDays': 100000},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['subscriptionDetail']['durationDays'], 30)


class GeneratedPasswordTests(APITestCase):
    def test_the_alphabet_cannot_be_mangled_by_html_escaping(self):
        """
        A generated password travels to the customer through a Django
        template, which escapes by default. Keeping the escapable characters
        out of the alphabet means the value that arrives is the value that was
        set, whichever part of the message they read it from.
        """
        from .services_users import generate_secure_password

        forbidden = set('&<>"\'')
        for _ in range(200):
            password = generate_secure_password()
            self.assertFalse(forbidden & set(password), password)

    def test_every_password_carries_all_four_character_classes(self):
        import string

        from .services_users import PASSWORD_LENGTH, generate_secure_password

        for _ in range(50):
            password = generate_secure_password()
            self.assertEqual(len(password), PASSWORD_LENGTH)
            self.assertTrue(any(character.islower() for character in password))
            self.assertTrue(any(character.isupper() for character in password))
            self.assertTrue(any(character.isdigit() for character in password))
            self.assertTrue(any(character in string.punctuation for character in password))

    def test_two_generated_passwords_are_never_the_same(self):
        from .services_users import generate_secure_password

        generated = {generate_secure_password() for _ in range(200)}
        self.assertEqual(len(generated), 200)


class AdminMeetingTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

        self.signup = SignupRequest.objects.create(
            user=self.customer,
            preferred_date=(timezone.now() + timedelta(days=3)).date(),
            preferred_time='10:00 AM',
        )
        from .services_meetings import create_meeting_from_signup

        self.meeting = create_meeting_from_signup(self.signup)

    def test_a_signup_slot_in_the_future_is_booked_outright(self):
        self.assertEqual(self.meeting.status, Meeting.SCHEDULED)
        self.assertIsNotNone(self.meeting.scheduled_at)
        self.assertEqual(self.meeting.scheduled_at.strftime('%H:%M'), '10:00')

    def test_an_unparsable_slot_leaves_the_meeting_for_an_admin_to_place(self):
        from .services_meetings import create_meeting_from_signup

        other = User.objects.create_user(
            email='vague@example.test',
            password=None,
            full_name='Vague Requester',
            firm_name='Vague Firm',
            country='Italy',
            is_active=False,
        )
        signup = SignupRequest.objects.create(
            user=other,
            preferred_date=(timezone.now() + timedelta(days=4)).date(),
            preferred_time='half two',
        )

        meeting = create_meeting_from_signup(signup)
        self.assertEqual(meeting.status, Meeting.REQUESTED)
        self.assertIsNone(meeting.scheduled_at)
        # The original wording is kept so somebody can act on it.
        self.assertEqual(meeting.requested_slot_label, 'half two')

    def test_rescheduling_into_the_past_is_refused(self):
        response = self.client.post(
            reverse('kraios_admin:meeting-reschedule', args=[self.meeting.pk]),
            {
                'date': (timezone.now() - timedelta(days=1)).date().isoformat(),
                'time': '10:00',
            },
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rescheduling_clears_a_reminder_that_was_already_sent(self):
        Meeting.objects.filter(pk=self.meeting.pk).update(
            user_reminder_sent_at=timezone.now()
        )

        response = self.client.post(
            reverse('kraios_admin:meeting-reschedule', args=[self.meeting.pk]),
            {
                'date': (timezone.now() + timedelta(days=5)).date().isoformat(),
                'time': '14:30',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['time'], '14:30')
        self.meeting.refresh_from_db()
        self.assertIsNone(self.meeting.user_reminder_sent_at)

    def test_a_successful_meeting_approves_the_signup_request(self):
        response = self.client.post(
            reverse('kraios_admin:meeting-status', args=[self.meeting.pk]),
            {'status': 'Completed', 'outcome': Meeting.OUTCOME_CONTINUING},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.signup.refresh_from_db()
        self.assertEqual(self.signup.status, SignupRequest.APPROVED)

    def test_a_declined_meeting_rejects_the_signup_request(self):
        self.client.post(
            reverse('kraios_admin:meeting-status', args=[self.meeting.pk]),
            {'status': 'Completed', 'outcome': Meeting.OUTCOME_NOT_CONTINUING},
            format='json',
        )

        self.signup.refresh_from_db()
        self.assertEqual(self.signup.status, SignupRequest.REJECTED)

    def test_scheduled_cannot_be_set_without_a_slot(self):
        response = self.client.post(
            reverse('kraios_admin:meeting-status', args=[self.meeting.pk]),
            {'status': 'Scheduled'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_booked_slot_is_offered_as_unavailable_rather_than_hidden(self):
        """
        A taken slot still appears, marked unavailable.

        Omitting it would leave the console unable to say WHY 11:00 is missing,
        and "there is no 11:00" reads as a configuration problem rather than as
        somebody else's booking.
        """
        monday = _next_weekday(timezone.now().date() + timedelta(days=1), weekday=0)
        booked_at = _at(monday, 11, 0)

        Meeting.objects.create(
            user=self.customer,
            scheduled_at=booked_at,
            status=Meeting.SCHEDULED,
        )

        response = self.client.get(
            reverse('kraios_admin:meeting-slots'),
            {'date': monday.isoformat()},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        slots = {slot['time']: slot['available'] for slot in response.data['slots']}

        self.assertIn('11:00', slots)
        self.assertFalse(slots['11:00'])
        self.assertTrue(slots['11:30'])

    def test_a_blacked_out_date_offers_nothing(self):
        from .models import AvailabilityBlackout

        monday = _next_weekday(timezone.now().date() + timedelta(days=1), weekday=0)
        AvailabilityBlackout.objects.create(date=monday, reason='Team offsite')

        response = self.client.get(
            reverse('kraios_admin:meeting-slots'),
            {'date': monday.isoformat()},
        )
        self.assertEqual(response.data['slots'], [])


class PublicContactFormTests(AdminTestCase):
    """
    The contact form, and the queue it lands in.

    The value being protected is the JOIN. A form that accepts a message and a
    console that lists it are two halves of one feature, and each half passing
    on its own is exactly the failure this covers: the payload has to arrive as
    a record the console can read, filter and triage.

    The other half is the ALLOWLIST. This is the only endpoint in the admin app
    an anonymous caller may write through, so what it refuses to accept matters
    as much as what it stores.
    """

    def setUp(self):
        super().setUp()
        self.contact_url = reverse('kraios_public:contact')
        self.payload = {
            'name': 'Sara Ali',
            'email': 'Sara@Studio.test',
            'firm': 'Ali Studio',
            'country': 'Pakistan',
            'topic': 'Technical issue',
            'subject': '3D render stalls on large plans',
            'message': 'A 240 sqm plan stalls at the rendering step and never finishes.',
        }

    def _send(self, **overrides):
        payload = {**self.payload, **overrides}
        return self.client.post(self.contact_url, payload, format='json')

    def test_a_message_reaches_the_console_queue_intact(self):
        created = self._send()
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertTrue(created.data['request_id'])

        self.authenticate()
        listed = self.client.get(reverse('kraios_admin:support-list'))
        self.assertEqual(listed.status_code, status.HTTP_200_OK)

        rows = listed.data['items']
        self.assertEqual(len(rows), 1)
        row = rows[0]

        # Every field the sender filled in, as the console reads it.
        self.assertEqual(row['name'], 'Sara Ali')
        self.assertEqual(row['firm'], 'Ali Studio')
        self.assertEqual(row['country'], 'Pakistan')
        self.assertEqual(row['topic'], 'Technical issue')
        self.assertEqual(row['subject'], self.payload['subject'])
        self.assertEqual(row['message'], self.payload['message'])
        # Normalised on the way in, so the account lookup on the detail page
        # cannot miss a match over capitals.
        self.assertEqual(row['email'], 'sara@studio.test')

    def test_the_sender_cannot_set_their_own_triage(self):
        # Not a hypothetical: `priority` is the one field on this record worth
        # forging, and the serializer is an allowlist precisely so it cannot be.
        created = self._send(
            topic='Partnerships',
            priority='Urgent',
            status='Resolved',
            assignee='someone@kraios.test',
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)

        self.authenticate()
        row = self.client.get(reverse('kraios_admin:support-list')).data['items'][0]

        self.assertEqual(row['status'], 'New')
        self.assertEqual(row['priority'], 'Medium')
        self.assertEqual(row['assignee'], 'Unassigned')

    def test_priority_is_derived_from_the_topic(self):
        self._send(topic='Technical issue')
        self._send(topic='Product demo', email='second@studio.test')

        self.authenticate()
        rows = self.client.get(reverse('kraios_admin:support-list')).data['items']
        by_topic = {row['topic']: row['priority'] for row in rows}

        self.assertEqual(by_topic['Technical issue'], 'High')
        self.assertEqual(by_topic['Product demo'], 'Medium')

    def test_a_topic_outside_the_vocabulary_is_refused(self):
        response = self._send(topic='Refund me now')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('topic', response.data)
        self.assertEqual(dummy_data.list_support_requests(), [])

    def test_a_message_too_short_to_action_is_refused(self):
        response = self._send(message='hi')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('message', response.data)
        self.assertEqual(dummy_data.list_support_requests(), [])

    def test_every_field_is_required(self):
        response = self.client.post(self.contact_url, {}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            set(response.data.keys()),
            {'name', 'email', 'firm', 'country', 'topic', 'subject', 'message'},
        )

    def test_the_console_can_filter_the_queue_by_topic(self):
        self._send(topic='Technical issue')
        self._send(topic='Partnerships', email='second@studio.test')

        self.authenticate()
        filtered = self.client.get(
            reverse('kraios_admin:support-list'), {'topic': 'Partnerships'}
        )

        self.assertEqual(len(filtered.data['items']), 1)
        self.assertEqual(filtered.data['items'][0]['topic'], 'Partnerships')

    def test_a_filed_request_can_be_triaged_like_any_other(self):
        # The queue must not have two kinds of record in it. A request that
        # arrived through the form has to answer the console's own PATCH.
        request_id = self._send().data['request_id']

        self.authenticate()
        response = self.client.patch(
            reverse('kraios_admin:support-detail', args=[request_id]),
            {'status': 'Open', 'assignee': self.admin_user.email},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'Open')
        self.assertEqual(response.data['assignee'], self.admin_user.email)

    def test_the_detail_page_links_a_sender_who_has_an_account(self):
        self._send(email=self.customer.email)

        self.authenticate()
        request_id = dummy_data.list_support_requests()[0]['id']
        detail = self.client.get(
            reverse('kraios_admin:support-detail', args=[request_id])
        )

        self.assertEqual(detail.data['accountId'], str(self.customer.pk))
        self.assertEqual(detail.data['country'], 'Pakistan')

    def test_the_form_needs_no_session_but_the_queue_does(self):
        # Public by necessity - a visitor has no account. Asserted so a later
        # default-permission change cannot quietly close the contact form, and
        # so the queue behind it never becomes public alongside it.
        self.assertEqual(self._send().status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            self.client.get(reverse('kraios_admin:support-list')).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )


class CustomerBillingTests(AdminTestCase):
    """
    What a customer reads of the console's catalogue — and what they must not.

    The value being protected is a SEAM, not an endpoint. The plans a customer
    is offered and the plans an administrator edits have to be one set of
    records; before these existed the customer dashboard read a hardcoded
    module and an administrator could rename, reprice or retire a plan without
    the pricing page moving. Every test here is therefore "the admin changed
    something, and the customer sees it" — or "the admin can see it and the
    customer cannot".
    """

    PLAN = {
        'name': 'Practice',
        'description': 'For a two-person practice.',
        'price': 95,
        'billingCycle': 'Monthly',
        'projectLimit': 8,
        'limit2d': 40,
        'limit3d': 20,
        'boqLimit': 20,
        'documentLimit': 80,
        'storageGb': 15,
        'features': ['8 projects a month'],
        'status': 'Active',
    }

    def setUp(self):
        super().setUp()
        self.plans_url = reverse('kraios_billing:plans')
        self.subscription_url = reverse('kraios_billing:subscription')

    def as_customer(self):
        self.client.force_authenticate(user=self.customer)

    def create_plan(self, **overrides):
        self.authenticate()
        response = self.client.post(
            reverse('kraios_admin:plan-list'), {**self.PLAN, **overrides}, format='json'
        )
        assert response.status_code == status.HTTP_201_CREATED, response.data
        self.client.logout()
        self.client.cookies.clear()
        return response.data

    def test_a_plan_an_administrator_creates_reaches_the_customer(self):
        created = self.create_plan()

        self.as_customer()
        response = self.client.get(self.plans_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {plan['name'] for plan in response.data['plans']}
        self.assertIn(created['name'], names)

    def test_an_inactive_plan_is_never_offered(self):
        self.create_plan(name='Retired Tier', status='Inactive')

        self.as_customer()
        names = {plan['name'] for plan in self.client.get(self.plans_url).data['plans']}

        self.assertNotIn('Retired Tier', names)

    def test_the_customer_is_not_shown_the_console_only_fields(self):
        # `status` is an administrative switch, `subscribers` is how many
        # accounts are on the plan, and `apiLimit` caps something nothing
        # meters. None of the three is a customer's to read.
        self.create_plan()

        self.as_customer()
        plan = self.client.get(self.plans_url).data['plans'][0]

        for withheld in ('status', 'subscribers', 'apiLimit'):
            self.assertNotIn(withheld, plan)

        self.assertEqual(
            set(plan.keys()),
            {'id', 'name', 'description', 'price', 'billingCycle', 'features', 'limits'},
        )

    def test_plans_are_returned_cheapest_first(self):
        self.create_plan(name='Dearer', price=500)
        self.create_plan(name='Cheaper', price=5)

        self.as_customer()
        prices = [plan['price'] for plan in self.client.get(self.plans_url).data['plans']]

        self.assertEqual(prices, sorted(prices))

    def test_an_account_with_no_plan_is_told_so_rather_than_given_one(self):
        # `null` is the answer, and the page that used to invent a "Premium
        # Pro" activation for every account is the reason this is asserted.
        self.as_customer()
        response = self.client.get(self.subscription_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data['subscription'])

    def test_the_customer_sees_the_subscription_an_administrator_assigned(self):
        plan = self.create_plan()

        self.authenticate()
        assigned = self.client.post(
            reverse('kraios_admin:user-subscription', args=[self.customer.pk]),
            {'planId': plan['id'], 'billingCycle': 'Monthly'},
            format='json',
        )
        self.assertEqual(assigned.status_code, status.HTTP_200_OK, assigned.data)
        self.client.logout()
        self.client.cookies.clear()

        self.as_customer()
        subscription = self.client.get(self.subscription_url).data['subscription']

        self.assertEqual(subscription['plan'], 'Practice')
        self.assertEqual(subscription['price'], 95)
        self.assertEqual(subscription['status'], 'Active')

        # Who granted it, when, and for how long are the console's record of an
        # administrative act. Leaking `assignedBy` would hand every customer an
        # employee's email address.
        for withheld in ('assignedBy', 'assignedAt', 'durationDays', 'isExpired'):
            self.assertNotIn(withheld, subscription)

    def test_an_expired_activation_reads_past_due_to_the_customer_too(self):
        # The console resolves status against today; the customer must be told
        # the same thing, not a stored 'ACTIVE' that stopped being true.
        plan = self.create_plan()

        self.authenticate()
        self.client.post(
            reverse('kraios_admin:user-subscription', args=[self.customer.pk]),
            {'planId': plan['id'], 'billingCycle': 'Monthly'},
            format='json',
        )
        self.client.logout()
        self.client.cookies.clear()

        dummy_data.expire_subscription_for_tests(self.customer.pk)

        self.as_customer()
        subscription = self.client.get(self.subscription_url).data['subscription']

        self.assertEqual(subscription['status'], 'Past Due')

    def test_usage_is_reported_against_the_plan_the_account_is_on(self):
        plan = self.create_plan()

        self.authenticate()
        self.client.post(
            reverse('kraios_admin:user-subscription', args=[self.customer.pk]),
            {'planId': plan['id'], 'billingCycle': 'Monthly'},
            format='json',
        )
        self.client.logout()
        self.client.cookies.clear()

        self.as_customer()
        response = self.client.get(reverse('kraios_billing:usage'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['hasPlan'])

        by_key = {row['key']: row for row in response.data['usage']}
        # The cap comes from the plan, not from anywhere this endpoint decides.
        self.assertEqual(by_key['projects']['limit'], self.PLAN['projectLimit'])
        self.assertEqual(by_key['projects']['used'], 0)
        self.assertEqual(by_key['projects']['remaining'], self.PLAN['projectLimit'])

    def test_the_unmetered_api_cap_is_not_reported_to_the_customer(self):
        # The plan payload withholds `apiLimit` because nothing counts API
        # requests; the usage payload has to agree, or the page shows a meter
        # for an allowance the pricing card never mentioned.
        plan = self.create_plan()

        self.authenticate()
        self.client.post(
            reverse('kraios_admin:user-subscription', args=[self.customer.pk]),
            {'planId': plan['id'], 'billingCycle': 'Monthly'},
            format='json',
        )
        self.client.logout()
        self.client.cookies.clear()

        self.as_customer()
        keys = {row['key'] for row in self.client.get(reverse('kraios_billing:usage')).data['usage']}

        self.assertNotIn('apiRequests', keys)
        self.assertIn('projects', keys)

    def test_an_account_with_no_plan_gets_counts_but_no_caps(self):
        # A meter drawn at 0% against a cap that does not exist reads as
        # "nothing used", which is the opposite of the truth — so an
        # unsubscribed account reports every limit as null and says so once.
        self.as_customer()
        response = self.client.get(reverse('kraios_billing:usage'))

        self.assertFalse(response.data['hasPlan'])
        self.assertIsNone(response.data['overallPercent'])
        self.assertTrue(response.data['usage'])
        for row in response.data['usage']:
            self.assertIsNone(row['limit'])
            self.assertIsNone(row['percent'])

    def test_usage_counts_only_the_callers_own_work(self):
        # The endpoint takes no id — it reads `request.user`. Asserted so it can
        # never grow one.
        from projects.models import Project

        Project.objects.create(owner=self.customer, name='Mine')
        Project.objects.create(owner=self.admin_user, name='Somebody else\'s')

        self.as_customer()
        by_key = {
            row['key']: row
            for row in self.client.get(reverse('kraios_billing:usage')).data['usage']
        }

        self.assertEqual(by_key['projects']['used'], 1)

    def test_billing_needs_a_session(self):
        for url in (self.plans_url, self.subscription_url, reverse('kraios_billing:usage')):
            self.assertEqual(
                self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED, url
            )

    def test_a_customer_cannot_reach_the_console_catalogue(self):
        # The same records, the other door. A customer session must not open it.
        self.as_customer()

        self.assertEqual(
            self.client.get(reverse('kraios_admin:plan-list')).status_code,
            status.HTTP_403_FORBIDDEN,
        )


class SetupStateTests(APITestCase):
    """
    The onboarding ladder, rung by rung.

    Pinned down because two of its states are both ``is_active=False`` and the
    difference between them - has this account ever been issued credentials -
    is the thing an administrator acts on.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            email='ladder@example.test',
            password=None,
            full_name='Ladder Case',
            firm_name='Ladder Firm',
            country='Norway',
            is_active=False,
        )

    def _state(self, meeting=None):
        from .services_users import derive_setup_state

        return derive_setup_state(self.user, meeting)

    def _meeting(self, status_key, outcome=Meeting.OUTCOME_PENDING):
        return Meeting(user=self.user, status=status_key, outcome=outcome)

    def test_a_signup_with_no_meeting_is_pending(self):
        self.assertEqual(self._state(), 'Signup Pending')

    def test_a_booked_call_is_awaiting_verification(self):
        self.assertEqual(self._state(self._meeting(Meeting.SCHEDULED)), 'Verification Pending')

    def test_a_cancelled_call_falls_back_to_pending_not_inactive(self):
        self.assertEqual(self._state(self._meeting(Meeting.CANCELLED)), 'Signup Pending')
        self.assertEqual(self._state(self._meeting(Meeting.NO_SHOW)), 'Signup Pending')

    def test_a_successful_call_makes_the_account_ready_for_credentials(self):
        meeting = self._meeting(Meeting.COMPLETED, Meeting.OUTCOME_CONTINUING)
        self.assertEqual(self._state(meeting), 'Ready')

    def test_a_customer_who_declined_reads_as_inactive(self):
        meeting = self._meeting(Meeting.COMPLETED, Meeting.OUTCOME_NOT_CONTINUING)
        self.assertEqual(self._state(meeting), 'Inactive')

    def test_issued_credentials_await_the_first_sign_in(self):
        self.user.set_password('IssuedPassw0rd!')
        self.user.is_active = True
        self.assertEqual(self._state(), 'Password Setup Pending')

    def test_a_signed_in_account_is_simply_active(self):
        self.user.set_password('IssuedPassw0rd!')
        self.user.is_active = True
        self.user.last_login = timezone.now()
        self.assertEqual(self._state(), 'Active')

    def test_a_deactivated_customer_is_not_mistaken_for_a_new_signup(self):
        self.user.set_password('IssuedPassw0rd!')
        self.user.is_active = False
        self.assertEqual(self._state(self._meeting(Meeting.SCHEDULED)), 'Inactive')


class MeetingReminderTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        lead = settings.KRAIOS_ADMIN_REMINDER_LEAD_MINUTES
        self.meeting = Meeting.objects.create(
            user=self.customer,
            scheduled_at=timezone.now() + timedelta(minutes=lead - 5),
            status=Meeting.SCHEDULED,
        )

    def test_a_due_meeting_is_reminded_once_however_often_the_task_runs(self):
        self.assertEqual(send_due_meeting_reminders(), 1)

        customer_messages = [
            message for message in mail.outbox if message.to == [self.customer.email]
        ]
        self.assertEqual(len(customer_messages), 1)

        # A second worker, a beat overlap, or a Celery redelivery.
        mail.outbox.clear()
        self.assertEqual(send_due_meeting_reminders(), 0)
        self.assertEqual(mail.outbox, [])

    def test_a_meeting_outside_the_window_is_not_reminded(self):
        Meeting.objects.filter(pk=self.meeting.pk).update(
            scheduled_at=timezone.now() + timedelta(days=2)
        )
        self.assertEqual(send_due_meeting_reminders(), 0)

    def test_a_cancelled_meeting_is_not_reminded(self):
        Meeting.objects.filter(pk=self.meeting.pk).update(status=Meeting.CANCELLED)
        self.assertEqual(send_due_meeting_reminders(), 0)


class MeetingBackfillTests(APITestCase):
    """
    The migration that gives pre-existing signups a meeting.

    Worth its own test because it is the one piece of this app that runs
    against data nobody can re-create: if it maps a status wrongly, the console
    reports the wrong onboarding state for every account that existed before.
    """

    def test_backfill_maps_each_signup_status_onto_a_meeting(self):
        import importlib

        from django.apps import apps as app_registry

        migration = importlib.import_module(
            'admin.migrations.0002_backfill_meetings_from_signups'
        )

        approved = self._signup('approved@example.test', SignupRequest.APPROVED, days=-40)
        self._signup('rejected@example.test', SignupRequest.REJECTED, days=-30)
        self._signup('upcoming@example.test', SignupRequest.PENDING, days=5)
        self._signup('stale@example.test', SignupRequest.PENDING, days=-5)
        self._signup('vague@example.test', SignupRequest.PENDING, days=5, slot='sometime')

        migration.backfill(app_registry, None)

        self.assertEqual(Meeting.objects.count(), 5)

        by_email = {
            meeting.user.email: meeting
            for meeting in Meeting.objects.select_related('user')
        }

        self.assertEqual(by_email['approved@example.test'].status, Meeting.COMPLETED)
        self.assertEqual(
            by_email['approved@example.test'].outcome, Meeting.OUTCOME_CONTINUING
        )
        self.assertEqual(by_email['rejected@example.test'].status, Meeting.COMPLETED)
        self.assertEqual(
            by_email['rejected@example.test'].outcome, Meeting.OUTCOME_NOT_CONTINUING
        )

        # Still ahead: keep the booking.
        self.assertEqual(by_email['upcoming@example.test'].status, Meeting.SCHEDULED)

        # Already past with nothing recorded: it needs re-placing, not a
        # silent claim that it happened.
        self.assertEqual(by_email['stale@example.test'].status, Meeting.REQUESTED)

        # Unparsable slot: the wording survives so somebody can act on it.
        self.assertIsNone(by_email['vague@example.test'].scheduled_at)
        self.assertEqual(by_email['vague@example.test'].requested_slot_label, 'sometime')

        # The original signup date is preserved, not stamped as "now".
        self.assertEqual(
            by_email['approved@example.test'].created_at.date(),
            approved.created_at.date(),
        )

        # Running it again adds nothing.
        migration.backfill(app_registry, None)
        self.assertEqual(Meeting.objects.count(), 5)

    def _signup(self, email, signup_status, *, days, slot='10:00 AM'):
        user = User.objects.create_user(
            email=email,
            password=None,
            full_name=email.split('@')[0].title(),
            firm_name='Legacy Firm',
            country='Portugal',
            is_active=False,
        )
        return SignupRequest.objects.create(
            user=user,
            preferred_date=(timezone.now() + timedelta(days=days)).date(),
            preferred_time=slot,
            status=signup_status,
        )


class PlanCatalogueTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_plans_are_listed_with_their_account_counts(self):
        response = self.client.get(reverse('kraios_admin:plan-list'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {plan['name'] for plan in response.data['items']}
        self.assertEqual(names, {'Starter', 'Studio', 'Enterprise'})

    # EXACTLY what `subscriptionService.toRecord` sends, field for field.
    #
    # Written out rather than built from a helper on purpose. The plan endpoints
    # had a test that passed while every save from the real console 400'd,
    # because the test sent `apiLimit` and the console has no field for it - a
    # fixture that spoke a dialect no client speaks. Anything added here has to
    # be added to the console too, and the diff will say so.
    CONSOLE_PLAN_PAYLOAD = {
        'name': 'Atelier',
        'description': 'For a small studio.',
        'price': 49,
        'billingCycle': 'Monthly',
        'projectLimit': 10,
        'limit2d': 50,
        'limit3d': 25,
        'boqLimit': 25,
        'documentLimit': 100,
        'storageGb': 20,
        'features': ['3D rendering'],
        'status': 'Active',
    }

    def test_the_console_can_create_a_plan_with_the_payload_it_actually_sends(self):
        response = self.client.post(
            reverse('kraios_admin:plan-list'),
            self.CONSOLE_PLAN_PAYLOAD,
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['name'], 'Atelier')
        self.assertEqual(response.data['projectLimit'], 10)
        # Nothing meters API requests, so an unsent cap starts at 0 - which
        # `metric_usage` reads as uncapped rather than as "none allowed".
        self.assertEqual(response.data['apiLimit'], 0)

    def test_editing_a_plan_does_not_wipe_a_limit_the_console_never_sends(self):
        # The regression that matters more than the 400: making `apiLimit`
        # optional must not turn "not mentioned" into "set to zero", or one save
        # from the console would silently uncap a seeded plan.
        before = dummy_data.get_plan('plan-studio')['apiLimit']
        self.assertGreater(before, 0)

        response = self.client.patch(
            reverse('kraios_admin:plan-detail', args=['plan-studio']),
            {**self.CONSOLE_PLAN_PAYLOAD, 'name': 'Studio', 'description': 'Edited.'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['description'], 'Edited.')
        self.assertEqual(response.data['apiLimit'], before)

    def test_a_limit_that_is_sent_is_still_validated(self):
        # Optional is not unchecked. A caller that names `apiLimit` gets the
        # same rules as any other cap.
        response = self.client.post(
            reverse('kraios_admin:plan-list'),
            {**self.CONSOLE_PLAN_PAYLOAD, 'apiLimit': -5},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('apiLimit', response.data['errors'])

    def test_a_required_limit_is_still_required(self):
        payload = {**self.CONSOLE_PLAN_PAYLOAD}
        del payload['projectLimit']

        response = self.client.post(
            reverse('kraios_admin:plan-list'), payload, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('projectLimit', response.data['errors'])

    def test_a_duplicate_plan_name_is_refused(self):
        response = self.client.post(
            reverse('kraios_admin:plan-list'),
            {
                'name': 'Studio',
                'description': 'A second one.',
                'price': 10,
                'billingCycle': 'Monthly',
                'status': 'Active',
                'projectLimit': 1,
                'limit2d': 1,
                'limit3d': 1,
                'boqLimit': 1,
                'documentLimit': 1,
                'apiLimit': 1,
                'storageGb': 1,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('name', response.data['errors'])

    def test_a_plan_with_accounts_on_it_cannot_be_deleted(self):
        self.client.post(
            reverse('kraios_admin:user-subscription', args=[self.customer.pk]),
            {'planId': 'plan-studio'},
            format='json',
        )

        response = self.client.delete(
            reverse('kraios_admin:plan-detail', args=['plan-studio'])
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('Studio', response.data['detail'])
        self.assertIsNotNone(dummy_data.get_plan('plan-studio'))

    def test_an_empty_plan_can_be_deleted(self):
        response = self.client.delete(
            reverse('kraios_admin:plan-detail', args=['plan-starter'])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(dummy_data.get_plan('plan-starter'))


class SlotParsingTests(APITestCase):
    """The public form and the console send different wordings for one thing."""

    def test_the_formats_both_applications_send_are_understood(self):
        self.assertEqual(parse_slot_label('09:00 AM').strftime('%H:%M'), '09:00')
        self.assertEqual(parse_slot_label('2:30 pm').strftime('%H:%M'), '14:30')
        self.assertEqual(parse_slot_label('14:00').strftime('%H:%M'), '14:00')
        self.assertEqual(
            parse_slot_label('10:00 AM - 11:00 AM').strftime('%H:%M'), '10:00'
        )

    def test_anything_else_is_refused_rather_than_guessed_at(self):
        for label in ('half two', 'morning', '', None, '25:00', '09:70'):
            self.assertIsNone(parse_slot_label(label), label)


class OnboardingJourneyTests(AdminTestCase):
    """
    The whole product flow, end to end, in one test.

    Signup books a call and grants nothing; the call is recorded; the
    administrator issues credentials; the account is put on a plan; and only
    then can the customer sign in. Each step is covered on its own elsewhere -
    this exists because the value of the flow is that the steps connect, and a
    regression in the joins between them would pass every other test here.
    """

    def test_a_visitor_becomes_a_paying_account(self):
        signup_url = reverse('accounts:signup-request')
        customer_login_url = reverse('accounts:login')
        # A Monday, not "in two days": the signup form only accepts a slot the
        # seeded Mon-Fri schedule actually offers, so a date picked by
        # arithmetic alone lands on a closed weekend one run in seven.
        booking_date = _next_weekday(timezone.now().date() + timedelta(days=1), weekday=0)

        # 1. The public form books a walkthrough.
        signup = self.client.post(
            signup_url,
            {
                'name': 'Tomas Ferreira',
                'firm': 'Estudio Norte',
                'email': 'tomas@estudionorte.test',
                'country': 'Portugal',
                'date': booking_date.isoformat(),
                'time': '10:00 AM',
            },
            format='json',
        )
        self.assertEqual(signup.status_code, status.HTTP_201_CREATED)

        applicant = User.objects.get(email='tomas@estudionorte.test')
        self.assertFalse(applicant.is_active)
        self.assertFalse(applicant.has_usable_password())

        meeting = Meeting.objects.get(user=applicant)
        self.assertEqual(meeting.status, Meeting.SCHEDULED)

        # 2. Signing up grants no access.
        refused = self.client.post(
            customer_login_url,
            {'email': applicant.email, 'password': 'anything-at-all'},
            format='json',
        )
        self.assertIn(
            refused.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

        # 3. The console shows the applicant waiting on the call.
        self.authenticate()
        listed = self.client.get(
            reverse('kraios_admin:user-list'), {'search': 'estudionorte'}
        )
        row = listed.data['items'][0]
        self.assertEqual(row['meetingStatus'], 'Scheduled')
        self.assertEqual(row['accountSetup'], 'Verification Pending')
        self.assertEqual(row['subscription'], 'None')

        # 4. The call happens and goes well.
        self.client.post(
            reverse('kraios_admin:meeting-status', args=[meeting.pk]),
            {'status': 'Completed', 'outcome': Meeting.OUTCOME_CONTINUING},
            format='json',
        )
        ready = self.client.get(
            reverse('kraios_admin:user-detail', args=[applicant.pk])
        )
        self.assertEqual(ready.data['accountSetup'], 'Ready')
        self.assertEqual(ready.data['signupStatus'], 'Approved')

        # 5. The administrator issues credentials.
        mail.outbox.clear()
        issued = self.client.post(
            reverse('kraios_admin:user-generate-password', args=[applicant.pk])
        )
        self.assertEqual(issued.status_code, status.HTTP_200_OK)
        self.assertTrue(issued.data['credentialsEmailSent'])
        self.assertEqual(issued.data['accountSetup'], 'Password Setup Pending')

        password = self._password_from_email(mail.outbox[-1].body)

        # 6. And puts the account on the plan agreed during the call.
        assigned = self.client.post(
            reverse('kraios_admin:user-subscription', args=[applicant.pk]),
            {'plan': 'Studio', 'billingCycle': 'Monthly'},
            format='json',
        )
        subscription = assigned.data['subscriptionDetail']
        self.assertEqual(subscription['plan'], 'Studio')
        self.assertEqual(subscription['status'], 'Active')
        self.assertEqual(
            subscription['renewalDate'],
            (timezone.now().date() + timedelta(days=30)).isoformat(),
        )

        # 7. Now, and only now, the customer can sign in.
        customer_client = APIClient()
        signed_in = customer_client.post(
            customer_login_url,
            {'email': applicant.email, 'password': password},
            format='json',
        )
        self.assertEqual(signed_in.status_code, status.HTTP_200_OK)

        # 8. Which the console reports as a live account.
        live = self.client.get(reverse('kraios_admin:user-detail', args=[applicant.pk]))
        self.assertEqual(live.data['accountStatus'], 'Active')
        self.assertEqual(live.data['accountSetup'], 'Active')
        self.assertIsNotNone(live.data['lastActive'])
        self.assertEqual(len(live.data['usageBreakdown']), 8)

    def _password_from_email(self, body):
        for line in body.splitlines():
            if line.startswith('Password:'):
                return line.split('Password:', 1)[1].strip()
        self.fail('The credentials email carried no password.')


class DashboardTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_the_overview_counts_real_accounts(self):
        response = self.client.get(reverse('kraios_admin:dashboard'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tiles = {tile['id']: tile for tile in response.data['metrics']}

        # One customer exists; the administrator is not one.
        self.assertEqual(tiles['total-users']['value'], '1')
        self.assertEqual(len(response.data['trends']['users']), response.data['days'])
