"""
Endpoints for the KRAIOS admin console.

Every view here does the same six things and little else: authenticate,
authorize the specific object, validate input, call one service, build the
response, map failures to a status code. Anything longer than that lives in a
``services_*`` module.

TWO BASE CLASSES DECIDE ACCESS, and they are the only place it is decided.
``AdminAPIView`` requires an active administrator and enforces CSRF through
``AdminCookieJWTAuthentication``; ``PublicAdminAPIView`` is the handful of
endpoints that must work before a session exists (CSRF cookie, sign in, token
refresh, sign out). A view that inherits from neither would be unreachable
rather than unprotected - there is no third option in ``urls.py``.

OBJECT SCOPE. Customer records are always fetched through
``services_users.customer_queryset()``, which excludes administrators. An
account holding an ``AdminProfile`` therefore 404s from every customer
endpoint: the console cannot edit, deactivate or issue credentials for an
administrator by guessing an id.

ERROR SHAPE. One key, everywhere: ``{"detail": "..."}`` for a message, with an
optional ``{"errors": {field: message}}`` beside it when the console should
mark fields. Internals - exception text, SQL, stack traces - never appear in a
response body; they go to the log.
"""
import logging
from datetime import date as date_type
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import OuterRef, Subquery
from django.http import Http404
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings as jwt_settings
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import SignupRequest
from accounts.views import enforce_csrf

from . import dummy_data, representations
from .audit import record_admin_action
from .authentication import AdminCookieJWTAuthentication
from .listing import (
    apply_sort,
    list_response,
    paginate_queryset,
    paginate_rows,
    read_filter,
    read_page_params,
    read_range,
    search_filter,
    sort_rows,
    window_start,
)
from .models import (
    AdminAuditLog,
    AvailabilityBlackout,
    AvailabilityRule,
    Meeting,
)
from .permissions import IsKraiosAdmin, IsPrivilegedAdmin, resolve_admin_profile
from .serializers import (
    AdminLoginSerializer,
    AdminPasswordChangeSerializer,
    AvailabilityBlackoutSerializer,
    AvailabilityWeekSerializer,
    MeetingNotesSerializer,
    MeetingRescheduleSerializer,
    MeetingStatusSerializer,
    PlanStatusSerializer,
    SubscriptionAssignSerializer,
    SupportTriageSerializer,
    UserStatusSerializer,
    UserUpdateSerializer,
)
from .services_auth import (
    AdminLoginError,
    AdminLoginLocked,
    change_admin_password,
    sign_in_admin,
    sign_out_admin,
    resolve_admin_for_refresh,
)
from .services_dashboard import build_dashboard_overview
from .services_meetings import (
    MAX_SLOT_HORIZON_DAYS,
    available_slots,
    combine_to_utc,
    latest_meeting_by_user,
    parse_slot_label,
    reschedule_meeting,
    set_meeting_status,
)
from .services_usage import (
    EMPTY_USAGE,
    USAGE_METRICS,
    USAGE_NEAR_LIMIT_PERCENT,
    overall_usage_percent,
    plan_limits,
    usage_breakdown,
    usage_by_user,
)
from .services_users import (
    UserOperationError,
    assign_subscription,
    clear_subscription,
    customer_queryset,
    derive_meeting_status,
    derive_setup_state,
    derive_signup_status,
    generate_user_password,
    resolve_plan_by_name,
    set_user_status,
    update_user,
)
from .throttles import AdminApiThrottle, AdminLoginThrottle
from .tokens import (
    ADMIN_TOKEN_SCOPE,
    SCOPE_CLAIM,
    SESSION_CLAIM,
    clear_admin_auth_cookies,
    issue_admin_tokens,
    set_admin_auth_cookies,
)


logger = logging.getLogger(__name__)

INVALID_SESSION_DETAIL = 'Your admin session has expired. Please sign in again.'


def error(message, http_status=status.HTTP_400_BAD_REQUEST, field_errors=None):
    payload = {'detail': message}
    if field_errors:
        payload['errors'] = field_errors
    return Response(payload, status=http_status)


class AdminAPIView(APIView):
    """Requires an active administrator. Every console endpoint uses this."""

    authentication_classes = [AdminCookieJWTAuthentication]
    permission_classes = [IsKraiosAdmin]
    throttle_classes = [AdminApiThrottle]

    @property
    def admin_profile(self):
        return resolve_admin_profile(self.request)


class PrivilegedAdminAPIView(AdminAPIView):
    permission_classes = [IsPrivilegedAdmin]


class PublicAdminAPIView(APIView):
    """Reachable without a session. Still CSRF-checked on every write."""

    authentication_classes = []
    permission_classes = [AllowAny]


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class AdminCsrfAPIView(PublicAdminAPIView):
    """Hands the console a CSRF cookie before it posts anything."""

    @method_decorator(ensure_csrf_cookie)
    def get(self, request):
        return Response({'detail': 'CSRF cookie set.', 'csrfToken': get_token(request)})


class AdminLoginAPIView(PublicAdminAPIView):
    throttle_classes = [AdminLoginThrottle]

    def post(self, request):
        enforce_csrf(request)

        serializer = AdminLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            admin_profile, access_token, refresh_token = sign_in_admin(
                request,
                serializer.validated_data['email'],
                serializer.validated_data['password'],
            )
        except AdminLoginLocked as locked:
            response = error(locked.message, locked.http_status)
            response['Retry-After'] = str(locked.retry_after)
            return response
        except AdminLoginError as failure:
            return error(failure.message, failure.http_status)

        response = Response({'admin': representations.serialize_admin_profile(admin_profile)})
        set_admin_auth_cookies(response, access_token, refresh_token)
        return response


class AdminRefreshAPIView(PublicAdminAPIView):
    """
    Exchange the refresh cookie for a new pair.

    Every entitlement is re-read from the database here rather than trusted
    from the token, so a refresh presented a minute after a deactivation mints
    nothing. The old refresh token is blacklisted, so a stolen copy is spent
    the moment the real console uses its own.
    """

    def post(self, request):
        enforce_csrf(request)

        raw_refresh = request.COOKIES.get(settings.ADMIN_AUTH_REFRESH_COOKIE_NAME)
        if not raw_refresh:
            return error(INVALID_SESSION_DETAIL, status.HTTP_401_UNAUTHORIZED)

        try:
            token = RefreshToken(raw_refresh)
        except TokenError:
            response = error(INVALID_SESSION_DETAIL, status.HTTP_401_UNAUTHORIZED)
            clear_admin_auth_cookies(response)
            return response

        if token.get(SCOPE_CLAIM) != ADMIN_TOKEN_SCOPE:
            response = error(INVALID_SESSION_DETAIL, status.HTTP_401_UNAUTHORIZED)
            clear_admin_auth_cookies(response)
            return response

        admin_profile = resolve_admin_for_refresh(
            token.get(jwt_settings.USER_ID_CLAIM),
            token.get(SESSION_CLAIM),
        )

        if admin_profile is None:
            response = error(INVALID_SESSION_DETAIL, status.HTTP_401_UNAUTHORIZED)
            clear_admin_auth_cookies(response)
            return response

        try:
            token.blacklist()
        except AttributeError:
            # Only reachable if the blacklist app is removed from settings.
            logger.warning('Refresh rotation without a blacklist backend.')

        access_token, refresh_token = issue_admin_tokens(admin_profile)
        response = Response(status=status.HTTP_204_NO_CONTENT)
        set_admin_auth_cookies(response, access_token, refresh_token)
        return response


class AdminLogoutAPIView(PublicAdminAPIView):
    """
    End the session.

    Deliberately not behind ``IsKraiosAdmin``: an administrator whose access
    token has already expired must still be able to sign out and have their
    cookies cleared, rather than being told they are not signed in while the
    browser still holds a refresh token.
    """

    def post(self, request):
        enforce_csrf(request)

        raw_refresh = request.COOKIES.get(settings.ADMIN_AUTH_REFRESH_COOKIE_NAME)
        if raw_refresh:
            try:
                token = RefreshToken(raw_refresh)
                if token.get(SCOPE_CLAIM) == ADMIN_TOKEN_SCOPE:
                    admin_profile = resolve_admin_for_refresh(
                        token.get(jwt_settings.USER_ID_CLAIM),
                        token.get(SESSION_CLAIM),
                    )
                    if admin_profile is not None:
                        sign_out_admin(admin_profile, request=request)
            except TokenError:
                # An unusable token is already as good as signed out.
                pass

        response = Response(status=status.HTTP_204_NO_CONTENT)
        clear_admin_auth_cookies(response)
        return response


class AdminMeAPIView(AdminAPIView):
    def get(self, request):
        return Response({'admin': representations.serialize_admin_profile(self.admin_profile)})


class AdminChangePasswordAPIView(AdminAPIView):
    def post(self, request):
        admin_profile = self.admin_profile
        serializer = AdminPasswordChangeSerializer(
            data=request.data,
            context={'admin_user': admin_profile.user},
        )
        serializer.is_valid(raise_exception=True)

        try:
            change_admin_password(
                admin_profile,
                serializer.validated_data['current_password'],
                serializer.validated_data['new_password'],
                request=request,
            )
        except AdminLoginError as failure:
            return error(failure.message, status.HTTP_400_BAD_REQUEST)

        # Every session died with the password, including this one.
        response = Response(
            {'detail': 'Password changed. Please sign in again.'},
            status=status.HTTP_200_OK,
        )
        clear_admin_auth_cookies(response)
        return response


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class DashboardOverviewAPIView(AdminAPIView):
    def get(self, request):
        return Response(build_dashboard_overview(read_range(request)))


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

USER_SORT_FIELDS = {
    'name': 'full_name',
    'firm': 'firm_name',
    'email': 'email',
    'country': 'country',
    'signupDate': 'date_joined',
    'lastActive': 'last_login',
    'accountStatus': 'is_active',
    'meetingStatus': 'latest_meeting_status',
}


def _latest_meeting_status_subquery():
    """
    The status of each account's most recent meeting, as one annotation.

    Filtering and sorting on "the latest meeting" has to happen in SQL or the
    pager lies - a Python-side filter after ``LIMIT`` would drop rows from a
    page instead of from the result.
    """
    return Subquery(
        Meeting.objects.filter(user=OuterRef('pk'))
        .order_by('-created_at')
        .values('status')[:1]
    )


def _build_user_rows(users):
    """
    Turn a page of ``User`` rows into console records.

    Every join is loaded once for the whole page - meetings, usage,
    subscriptions, plans, signup requests - so the row builder issues no
    queries and adding a field cannot make the endpoint N+1.
    """
    users = list(users)
    if not users:
        return []

    user_ids = [user.pk for user in users]
    meetings = latest_meeting_by_user(user_ids)
    usage_map = usage_by_user(user_ids)
    subscriptions = dummy_data.all_subscriptions()
    plans = {plan['id']: plan for plan in dummy_data.list_plans()}
    signups = {
        signup.user_id: signup
        for signup in SignupRequest.objects.filter(user_id__in=user_ids)
    }

    rows = []
    for user in users:
        subscription = subscriptions.get(str(user.pk))
        plan = plans.get(subscription['planId']) if subscription else None
        usage = usage_map.get(user.pk, dict(EMPTY_USAGE))
        meeting = meetings.get(user.pk)

        rows.append(
            representations.serialize_user(
                user,
                latest_meeting=meeting,
                signup_request=signups.get(user.pk),
                usage=usage,
                subscription=subscription,
                usage_percent=overall_usage_percent(usage, plan),
                setup_state=derive_setup_state(user, meeting),
                signup_status=derive_signup_status(signups.get(user.pk)),
                meeting_status=derive_meeting_status(meeting),
            )
        )
    return rows


class UserListAPIView(AdminAPIView):
    SEARCH_FIELDS = ('full_name', 'email', 'firm_name')

    def get(self, request):
        queryset = customer_queryset().annotate(
            latest_meeting_status=_latest_meeting_status_subquery()
        )

        query = request.query_params.get('search') or request.query_params.get('q')
        queryset = queryset.filter(search_filter(query, self.SEARCH_FIELDS))

        account_status = read_filter(request, 'account_status')
        if account_status in ('Active', 'Inactive'):
            queryset = queryset.filter(is_active=account_status == 'Active')

        meeting_status = read_filter(request, 'meeting_status')
        if meeting_status:
            queryset = self._filter_by_meeting_status(queryset, meeting_status)

        subscription = read_filter(request, 'subscription')
        if subscription:
            queryset = self._filter_by_subscription(queryset, subscription)

        setup_state = read_filter(request, 'account_setup')

        queryset = apply_sort(
            queryset,
            request.query_params.get('sort'),
            request.query_params.get('direction'),
            USER_SORT_FIELDS,
            'name',
        )

        page, page_size = read_page_params(request)

        # `account_setup` is derived from the account AND its latest meeting,
        # so it cannot be expressed as a column filter. It is applied after the
        # rows are built, which means it is the one filter that must page in
        # memory - hence the separate branch rather than a silently wrong pager.
        if setup_state:
            rows = [
                row for row in _build_user_rows(queryset)
                if row['accountSetup'] == setup_state
            ]
            rows, pagination = paginate_rows(rows, page, page_size)
            return Response(list_response(rows, pagination))

        users, pagination = paginate_queryset(queryset, page, page_size)
        return Response(list_response(_build_user_rows(users), pagination))

    def _filter_by_meeting_status(self, queryset, meeting_status):
        if meeting_status == 'Not Booked':
            return queryset.filter(latest_meeting_status__isnull=True)

        keys = {label: key for key, label in Meeting.STATUS_CHOICES}
        key = keys.get(meeting_status, meeting_status)
        return queryset.filter(latest_meeting_status=key)

    def _filter_by_subscription(self, queryset, subscription):
        """
        Narrow by plan.

        Subscriptions live in the placeholder store, so the plan's members are
        resolved to a set of ids and applied as one ``IN`` - the filtering
        still happens in SQL and the pager still counts the right rows.
        """
        subscriptions = dummy_data.all_subscriptions()

        if subscription == 'None':
            assigned = [
                int(user_id) for user_id in subscriptions if str(user_id).isdigit()
            ]
            return queryset.exclude(pk__in=assigned)

        matching = [
            int(user_id)
            for user_id, entry in subscriptions.items()
            if str(user_id).isdigit() and entry.get('plan') == subscription
        ]
        return queryset.filter(pk__in=matching)


def _get_customer(user_id):
    """
    One customer account, or 404.

    Administrators are outside this queryset, so asking for one by id is
    answered exactly as asking for a user that does not exist - the console
    cannot discover which ids are administrators by probing.
    """
    return get_object_or_404(customer_queryset(), pk=user_id)


def build_user_detail(user):
    """
    One account with everything the details page renders.

    It is ONE payload rather than five endpoints because that is what the page
    needs to draw - five calls would give it five loading states to coordinate
    for a screen that is meaningless in pieces. It is a module function rather
    than a view method so every endpoint that changes an account can return the
    account's new state without instantiating a view to borrow its formatter.
    """
    meeting = latest_meeting_by_user([user.pk]).get(user.pk)
    usage = usage_by_user([user.pk]).get(user.pk, dict(EMPTY_USAGE))
    subscription = dummy_data.get_subscription(user.pk)
    plan = dummy_data.get_plan(subscription['planId']) if subscription else None
    signup = SignupRequest.objects.filter(user=user).first()

    payload = representations.serialize_user(
        user,
        latest_meeting=meeting,
        signup_request=signup,
        usage=usage,
        subscription=subscription,
        usage_percent=overall_usage_percent(usage, plan),
        setup_state=derive_setup_state(user, meeting),
        signup_status=derive_signup_status(signup),
        meeting_status=derive_meeting_status(meeting),
    )

    payload['usageBreakdown'] = usage_breakdown(usage, plan)
    payload['meeting'] = (
        representations.serialize_meeting(meeting, include_user=False) if meeting else None
    )
    payload['meetings'] = [
        representations.serialize_meeting(row, include_user=False)
        for row in Meeting.objects.filter(user=user).order_by('-created_at')[:10]
    ]
    payload['plan'] = representations.serialize_plan(plan) if plan else None
    return payload


class UserDetailAPIView(AdminAPIView):
    def get(self, request, user_id):
        user = _get_customer(user_id)
        return Response(build_user_detail(user))

    def patch(self, request, user_id):
        user = _get_customer(user_id)

        serializer = UserUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            update_user(user, serializer.to_model_changes(), self.admin_profile, request=request)

            if 'accountStatus' in data:
                set_user_status(user, data['accountStatus'], self.admin_profile, request=request)

            if 'subscription' in data:
                self._apply_subscription_change(user, data['subscription'])
        except UserOperationError as failure:
            return error(failure.message, failure.http_status, failure.field_errors)

        user.refresh_from_db()
        return Response(build_user_detail(user))

    def _apply_subscription_change(self, user, plan_name):
        """
        Move the account onto a different plan, if it is actually different.

        Editing a name must not restart somebody's activation period, so a
        no-op selection is a no-op here too.
        """
        current = dummy_data.get_subscription(user.pk)
        current_name = (current or {}).get('plan') or 'None'
        requested = plan_name or 'None'

        if requested == current_name:
            return

        if requested == 'None':
            clear_subscription(user, self.admin_profile, request=self.request)
            return

        plan = resolve_plan_by_name(requested)
        if plan is None:
            raise UserOperationError(
                'That plan is not in the catalogue.',
                field_errors={'subscription': 'Unknown plan'},
            )

        assign_subscription(
            user,
            plan['id'],
            (current or {}).get('billingCycle') or plan['billingCycle'],
            (current or {}).get('durationDays') or dummy_data.DEFAULT_SUBSCRIPTION_DAYS,
            self.admin_profile,
            request=self.request,
        )

class UserStatusAPIView(AdminAPIView):
    def post(self, request, user_id):
        user = _get_customer(user_id)
        serializer = UserStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            set_user_status(user, serializer.validated_data['status'], self.admin_profile, request=request)
        except UserOperationError as failure:
            return error(failure.message, failure.http_status, failure.field_errors)

        user.refresh_from_db()
        return Response(build_user_detail(user))


class UserGeneratePasswordAPIView(AdminAPIView):
    """
    Issue credentials for an account and activate it.

    This is the gate between "had a good meeting" and "can sign in", so it is
    its own endpoint with its own audit entry rather than a field on the edit
    form. The generated password is emailed and is NOT in the response.
    """

    def post(self, request, user_id):
        user = _get_customer(user_id)

        try:
            user, email_sent = generate_user_password(user, self.admin_profile, request=request)
        except UserOperationError as failure:
            return error(failure.message, failure.http_status, failure.field_errors)

        payload = build_user_detail(user)
        payload['credentialsEmailSent'] = email_sent
        return Response(payload)


class UserSubscriptionAPIView(AdminAPIView):
    def post(self, request, user_id):
        user = _get_customer(user_id)

        serializer = SubscriptionAssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        plan_id = data.get('planId')
        if not plan_id:
            plan = resolve_plan_by_name(data.get('plan'))
            if plan is None:
                return error(
                    'That plan is not in the catalogue.',
                    field_errors={'plan': 'Unknown plan'},
                )
            plan_id = plan['id']

        try:
            assign_subscription(
                user,
                plan_id,
                data['billingCycle'],
                data['durationDays'],
                self.admin_profile,
                request=request,
            )
        except UserOperationError as failure:
            return error(failure.message, failure.http_status, failure.field_errors)

        return Response(build_user_detail(user))

    def delete(self, request, user_id):
        user = _get_customer(user_id)
        clear_subscription(user, self.admin_profile, request=request)
        return Response(build_user_detail(user))


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------

MEETING_SORT_FIELDS = {
    'user': 'user__full_name',
    'firm': 'user__firm_name',
    'email': 'user__email',
    'date': 'scheduled_at',
    'time': 'scheduled_at',
    'status': 'status',
    'createdDate': 'created_at',
}


class MeetingListAPIView(AdminAPIView):
    SEARCH_FIELDS = ('user__full_name', 'user__email', 'user__firm_name')

    def get(self, request):
        queryset = Meeting.objects.select_related('user')

        query = request.query_params.get('search') or request.query_params.get('q')
        queryset = queryset.filter(search_filter(query, self.SEARCH_FIELDS))

        meeting_status = read_filter(request, 'status')
        if meeting_status:
            keys = {label: key for key, label in Meeting.STATUS_CHOICES}
            queryset = queryset.filter(status=keys.get(meeting_status, meeting_status))

        firm = read_filter(request, 'firm')
        if firm:
            queryset = queryset.filter(user__firm_name=firm)

        date_window = read_filter(request, 'date')
        if date_window:
            start = window_start(date_window)
            if start is not None:
                # A meeting with no agreed slot is outside every window rather
                # than swept into all of them.
                queryset = queryset.filter(scheduled_at__gte=start)

        queryset = apply_sort(
            queryset,
            request.query_params.get('sort'),
            request.query_params.get('direction') or 'desc',
            MEETING_SORT_FIELDS,
            'createdDate',
        )

        page, page_size = read_page_params(request)
        meetings, pagination = paginate_queryset(queryset, page, page_size)

        return Response(
            list_response(
                [representations.serialize_meeting(meeting) for meeting in meetings],
                pagination,
            )
        )


def _get_meeting(meeting_id):
    return get_object_or_404(Meeting.objects.select_related('user'), pk=meeting_id)


class MeetingDetailAPIView(AdminAPIView):
    def get(self, request, meeting_id):
        return Response(representations.serialize_meeting(_get_meeting(meeting_id)))

    def patch(self, request, meeting_id):
        meeting = _get_meeting(meeting_id)
        serializer = MeetingNotesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        meeting.notes = serializer.validated_data['notes']
        meeting.updated_by = self.admin_profile
        meeting.save(update_fields=['notes', 'updated_by', 'updated_at'])

        record_admin_action(
            self.admin_profile,
            'meeting.notes',
            target_type='meeting',
            target_id=meeting.pk,
            summary=f'Updated notes on the call with {meeting.user.email}',
            request=request,
        )
        return Response(representations.serialize_meeting(meeting))


class MeetingStatusAPIView(AdminAPIView):
    def post(self, request, meeting_id):
        meeting = _get_meeting(meeting_id)
        serializer = MeetingStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            set_meeting_status(
                meeting,
                data['status'],
                self.admin_profile,
                outcome=data.get('outcome'),
                notes=data.get('notes'),
                request=request,
            )
        except ValueError:
            return error('That status cannot be set here.')

        return Response(representations.serialize_meeting(meeting))


class MeetingRescheduleAPIView(AdminAPIView):
    def post(self, request, meeting_id):
        meeting = _get_meeting(meeting_id)
        serializer = MeetingRescheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        slot_time = parse_slot_label(data['time'])
        if slot_time is None:
            return error(
                'That is not a time we can book.',
                field_errors={'time': 'Use a 24-hour HH:MM slot'},
            )

        scheduled_at = combine_to_utc(data['date'], slot_time)
        if scheduled_at <= timezone.now():
            # Rescheduling into the past is always a mistake, and the
            # confirmation dialog for it would be longer than the rule.
            return error(
                'A meeting cannot be booked in the past.',
                field_errors={'date': 'Choose a future date and time'},
            )

        reschedule_meeting(meeting, scheduled_at, self.admin_profile, request=request)

        if data.get('notes') is not None:
            meeting.notes = data['notes']
            meeting.save(update_fields=['notes', 'updated_at'])

        return Response(representations.serialize_meeting(meeting))


class MeetingSlotsAPIView(AdminAPIView):
    """The bookable slots on one date, from the administrator's availability."""

    def get(self, request):
        raw_date = request.query_params.get('date')
        if not raw_date:
            return error('Name the date to look at.', field_errors={'date': 'Required'})

        try:
            target = date_type.fromisoformat(raw_date)
        except ValueError:
            return error('That is not a date.', field_errors={'date': 'Use YYYY-MM-DD'})

        today = timezone.now().date()
        if target > today + timedelta(days=MAX_SLOT_HORIZON_DAYS):
            return error(
                f'Slots are only published {MAX_SLOT_HORIZON_DAYS} days ahead.',
                field_errors={'date': 'Beyond the booking horizon'},
            )

        return Response({'date': target.isoformat(), 'slots': available_slots(target)})


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def _availability_rule_fields(entry):
    """Map one validated availability entry onto model field names."""
    return {
        'weekday': entry['weekday'],
        'start_time': entry['startTime'],
        'end_time': entry['endTime'],
        'slot_minutes': entry['slotMinutes'],
        'is_active': entry['isActive'],
    }


class AvailabilityAPIView(AdminAPIView):
    def get(self, request):
        return Response({
            'rules': [
                representations.serialize_availability_rule(rule)
                for rule in AvailabilityRule.objects.all()
            ],
            'blackouts': [
                representations.serialize_blackout(blackout)
                for blackout in AvailabilityBlackout.objects.all()
            ],
        })

    def put(self, request):
        """
        Replace the whole weekly schedule in one write.

        A full replacement rather than per-row edits, because the schedule is
        one document to whoever is editing it: sending five separate requests
        to move a Tuesday would leave the week half-changed if the third failed.
        """
        serializer = AvailabilityWeekSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        rule_payloads = [
            _availability_rule_fields(entry)
            for entry in serializer.validated_data['rules']
        ]

        with transaction.atomic():
            AvailabilityRule.objects.all().delete()
            AvailabilityRule.objects.bulk_create(
                [AvailabilityRule(**fields) for fields in rule_payloads]
            )

        record_admin_action(
            self.admin_profile,
            'availability.replace',
            target_type='availability',
            summary=f'Set {len(rule_payloads)} availability windows',
            request=request,
        )
        return self.get(request)


class AvailabilityBlackoutAPIView(AdminAPIView):
    def post(self, request):
        serializer = AvailabilityBlackoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        blackout, _created = AvailabilityBlackout.objects.update_or_create(
            date=data['date'],
            defaults={'reason': data.get('reason', '')},
        )

        record_admin_action(
            self.admin_profile,
            'availability.blackout_add',
            target_type='availability',
            target_id=blackout.pk,
            summary=f'Blocked {blackout.date.isoformat()}',
            request=request,
        )
        return Response(
            representations.serialize_blackout(blackout),
            status=status.HTTP_201_CREATED,
        )


class AvailabilityBlackoutDetailAPIView(AdminAPIView):
    def delete(self, request, blackout_id):
        blackout = get_object_or_404(AvailabilityBlackout, pk=blackout_id)
        blocked_date = blackout.date.isoformat()
        blackout.delete()

        record_admin_action(
            self.admin_profile,
            'availability.blackout_remove',
            target_type='availability',
            target_id=blackout_id,
            summary=f'Unblocked {blocked_date}',
            request=request,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Subscription plans (placeholder catalogue - see dummy_data)
# ---------------------------------------------------------------------------

PLAN_SORT_FIELDS = {
    'name': 'name',
    'price': 'price',
    'status': 'status',
    'subscribers': 'subscribers',
    'projectLimit': 'projectLimit',
    'limit2d': 'limit2d',
    'limit3d': 'limit3d',
    'boqLimit': 'boqLimit',
    'documentLimit': 'documentLimit',
    'apiLimit': 'apiLimit',
    'storageGb': 'storageGb',
}


class PlanListAPIView(AdminAPIView):
    def get(self, request):
        plans = [representations.serialize_plan(plan) for plan in dummy_data.list_plans()]

        plan_status = read_filter(request, 'status')
        if plan_status:
            plans = [plan for plan in plans if plan['status'] == plan_status]

        plans = sort_rows(
            plans,
            request.query_params.get('sort'),
            request.query_params.get('direction'),
            PLAN_SORT_FIELDS,
            'price',
        )

        page, page_size = read_page_params(request)
        rows, pagination = paginate_rows(plans, page, page_size)
        return Response(list_response(rows, pagination))

    def post(self, request):
        plan, errors = dummy_data.create_plan(request.data)
        if errors:
            return error('That plan could not be saved.', field_errors=errors)

        record_admin_action(
            self.admin_profile,
            'plan.create',
            target_type='plan',
            target_id=plan['id'],
            summary=f'Created the {plan["name"]} plan',
            request=request,
        )
        return Response(
            representations.serialize_plan(plan),
            status=status.HTTP_201_CREATED,
        )


class PlanDetailAPIView(AdminAPIView):
    def get(self, request, plan_id):
        plan = dummy_data.get_plan(plan_id)
        if plan is None:
            raise Http404
        return Response(representations.serialize_plan(plan))

    def patch(self, request, plan_id):
        plan, errors = dummy_data.update_plan(plan_id, request.data)
        if errors:
            if 'detail' in errors:
                raise Http404
            return error('That plan could not be saved.', field_errors=errors)

        record_admin_action(
            self.admin_profile,
            'plan.update',
            target_type='plan',
            target_id=plan['id'],
            summary=f'Updated the {plan["name"]} plan',
            request=request,
        )
        return Response(representations.serialize_plan(plan))

    def delete(self, request, plan_id):
        result, errors = dummy_data.delete_plan(plan_id)
        if errors:
            # The store refuses while accounts are still on the plan, and the
            # refusal names the count so the console can offer deactivation
            # instead of a dead end.
            return error(errors['detail'], status.HTTP_409_CONFLICT)

        record_admin_action(
            self.admin_profile,
            'plan.delete',
            target_type='plan',
            target_id=plan_id,
            summary='Deleted a plan',
            request=request,
        )
        return Response(result)


class PlanStatusAPIView(AdminAPIView):
    def post(self, request, plan_id):
        serializer = PlanStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        plan, errors = dummy_data.set_plan_status(plan_id, serializer.validated_data['status'])
        if errors:
            raise Http404

        record_admin_action(
            self.admin_profile,
            'plan.status',
            target_type='plan',
            target_id=plan['id'],
            summary=f'{plan["name"]} set to {plan["status"]}',
            request=request,
        )
        return Response(representations.serialize_plan(plan))


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

USAGE_SORT_FIELDS = {
    'name': 'name',
    'firm': 'firm',
    'subscription': 'subscription',
    'usagePercent': 'usagePercent',
    **{metric['key']: metric['key'] for metric in USAGE_METRICS},
}


class UsageListAPIView(AdminAPIView):
    """
    Per-account usage.

    Sorting happens in memory because ``usagePercent`` and every metric are
    computed from aggregates rather than stored - there is no column to order
    by. The aggregates themselves are still a fixed number of queries, so the
    cost is one pass over the account list, not one query per account.
    """

    def get(self, request):
        queryset = customer_queryset()

        query = request.query_params.get('search') or request.query_params.get('q')
        queryset = queryset.filter(search_filter(query, ('full_name', 'email', 'firm_name')))

        firm = read_filter(request, 'firm')
        if firm:
            queryset = queryset.filter(firm_name=firm)

        users = list(queryset.order_by('full_name'))
        user_ids = [user.pk for user in users]
        usage_map = usage_by_user(user_ids)
        subscriptions = dummy_data.all_subscriptions()
        plans = {plan['id']: plan for plan in dummy_data.list_plans()}

        subscription_filter = read_filter(request, 'subscription')
        usage_filter = read_filter(request, 'status')

        rows = []
        for user in users:
            subscription = subscriptions.get(str(user.pk))
            plan = plans.get(subscription['planId']) if subscription else None
            usage = usage_map.get(user.pk, dict(EMPTY_USAGE))
            percent = overall_usage_percent(usage, plan)

            row = representations.serialize_usage_row(
                user,
                usage=usage,
                limits=plan_limits(plan),
                usage_percent=percent,
                subscription=subscription,
            )

            if subscription_filter and row['subscription'] != subscription_filter:
                continue
            if usage_filter == 'near' and percent < USAGE_NEAR_LIMIT_PERCENT:
                continue
            if usage_filter == 'normal' and percent >= USAGE_NEAR_LIMIT_PERCENT:
                continue

            rows.append(row)

        rows = sort_rows(
            rows,
            request.query_params.get('sort'),
            request.query_params.get('direction') or 'desc',
            USAGE_SORT_FIELDS,
            'usagePercent',
        )

        page, page_size = read_page_params(request)
        paged, pagination = paginate_rows(rows, page, page_size)
        return Response(list_response(paged, pagination))


class UsageFirmsAPIView(AdminAPIView):
    """Every firm that appears on an account, for the firm filter."""

    def get(self, request):
        firms = (
            customer_queryset()
            .exclude(firm_name='')
            .values_list('firm_name', flat=True)
            .distinct()
            .order_by('firm_name')
        )
        return Response({'items': list(firms)})


# ---------------------------------------------------------------------------
# Support (placeholder queue - see dummy_data)
# ---------------------------------------------------------------------------

SUPPORT_SORT_FIELDS = {
    'name': 'name',
    'firm': 'firm',
    'topic': 'topic',
    'subject': 'subject',
    'submittedAt': 'submittedAt',
    'status': 'status',
    'priority': 'priority',
}


class SupportListAPIView(AdminAPIView):
    def get(self, request):
        rows = dummy_data.list_support_requests()

        query = (request.query_params.get('search') or '').strip().lower()
        if query:
            rows = [
                row for row in rows
                if any(
                    query in str(row.get(field, '')).lower()
                    for field in ('name', 'email', 'firm', 'country', 'topic', 'subject')
                )
            ]

        for parameter, field in (
            ('status', 'status'),
            ('priority', 'priority'),
            ('topic', 'topic'),
        ):
            value = read_filter(request, parameter)
            if value:
                rows = [row for row in rows if row.get(field) == value]

        rows = sort_rows(
            rows,
            request.query_params.get('sort'),
            request.query_params.get('direction') or 'desc',
            SUPPORT_SORT_FIELDS,
            'submittedAt',
        )

        page, page_size = read_page_params(request)
        paged, pagination = paginate_rows(rows, page, page_size)
        return Response(
            list_response(
                [representations.serialize_support_request(row) for row in paged],
                pagination,
            )
        )


class SupportDetailAPIView(AdminAPIView):
    def get(self, request, request_id):
        row = dummy_data.get_support_request(request_id)
        if row is None:
            raise Http404
        return Response(self._payload(row))

    def patch(self, request, request_id):
        serializer = SupportTriageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        row, errors = dummy_data.update_support_request(
            request_id,
            status=data.get('status'),
            priority=data.get('priority'),
            assignee=data.get('assignee'),
        )
        if errors:
            raise Http404

        record_admin_action(
            self.admin_profile,
            'support.triage',
            target_type='support',
            target_id=request_id,
            summary=f'Triaged "{row["subject"]}"',
            metadata={key: value for key, value in data.items()},
            request=request,
        )
        return Response(self._payload(row))

    def _payload(self, row):
        """
        The request, plus the account it came from when there is one.

        A contact form can be filled in by somebody who has never signed up, so
        the account link is genuinely optional and the page has to render
        without it.
        """
        account = customer_queryset().filter(email__iexact=row['email']).first()
        return representations.serialize_support_request(
            row,
            account_id=str(account.pk) if account else None,
        )


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

class AuditLogAPIView(PrivilegedAdminAPIView):
    """
    Who did what.

    Super Admin only: an audit trail readable by everyone it audits is a
    formality rather than a control.
    """

    def get(self, request):
        queryset = AdminAuditLog.objects.all()

        action = read_filter(request, 'action')
        if action:
            queryset = queryset.filter(action=action)

        target_type = read_filter(request, 'target_type')
        if target_type:
            queryset = queryset.filter(target_type=target_type)

        admin_email = read_filter(request, 'admin_email')
        if admin_email:
            queryset = queryset.filter(admin_email__iexact=admin_email)

        page, page_size = read_page_params(request)
        entries, pagination = paginate_queryset(queryset, page, page_size)
        return Response(
            list_response(
                [representations.serialize_audit_entry(entry) for entry in entries],
                pagination,
            )
        )
