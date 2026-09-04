import logging
import uuid
from datetime import date as date_type
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.models import update_last_login
from django.db import transaction
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status
from rest_framework.authentication import CSRFCheck
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from .emails import send_signup_meeting_confirmation
from .models import User
from .serializers import (
    ForgotPasswordConfirmSerializer,
    ForgotPasswordRequestSerializer,
    LoginSerializer,
    SignupRequestSerializer,
    UserProfileSerializer,
)
from profiles.models import AccountVerification
from profiles.services import (
    blacklist_user_refresh_tokens,
    consume_account_verification,
    create_account_verification,
)
from profiles.throttles import PasswordResetThrottle

# The booking calendar is owned by the admin app - it is the administrator's
# schedule - and read here rather than reimplemented, so the public form and
# the console can never offer different weeks. Safe at module scope: nothing
# under `admin.services_meetings` imports `accounts.views` back.
from admin.services_meetings import (
    MAX_SLOT_HORIZON_DAYS,
    available_slots,
    format_slot_label,
    open_days,
    parse_slot_label,
)

from .throttles import PublicBookingThrottle


logger = logging.getLogger(__name__)


def enforce_csrf(request):
    csrf_check = CSRFCheck(lambda request: None)
    csrf_check.process_request(request)
    reason = csrf_check.process_view(request, None, (), {})

    if reason:
        raise PermissionDenied(f'CSRF Failed: {reason}')


def set_auth_cookies(response, access_token, refresh_token=None):
    response.set_cookie(
        settings.AUTH_ACCESS_COOKIE_NAME,
        access_token,
        max_age=settings.AUTH_ACCESS_COOKIE_AGE,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        path='/',
    )

    if refresh_token is not None:
        response.set_cookie(
            settings.AUTH_REFRESH_COOKIE_NAME,
            refresh_token,
            max_age=settings.AUTH_REFRESH_COOKIE_AGE,
            httponly=True,
            secure=settings.AUTH_COOKIE_SECURE,
            samesite=settings.AUTH_COOKIE_SAMESITE,
            path='/',
        )


def clear_auth_cookies(response):
    response.delete_cookie(
        settings.AUTH_ACCESS_COOKIE_NAME,
        path='/',
        samesite=settings.AUTH_COOKIE_SAMESITE,
    )
    response.delete_cookie(
        settings.AUTH_REFRESH_COOKIE_NAME,
        path='/',
        samesite=settings.AUTH_COOKIE_SAMESITE,
    )


class SignupRequestAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = SignupRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        signup_request = serializer.save()

        try:
            email_sent = bool(send_signup_meeting_confirmation(signup_request))
        except Exception:
            email_sent = False
            logger.exception(
                'Could not send signup confirmation email for request %s.',
                signup_request.id,
            )

        response_data = {
            'message': 'Sign-up request submitted successfully.',
            'request_id': signup_request.id,
            'status': signup_request.status,
            'confirmation_email_sent': email_sent,
        }
        return Response(response_data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Public booking calendar
# ---------------------------------------------------------------------------
#
# The signup form draws its calendar and its slot list from these two, so the
# dates and times a visitor can pick are exactly the ones an administrator left
# open in the console. They are the PUBLIC read side of
# ``admin.services_meetings``; the console keeps its own authenticated
# ``meetings/slots/`` because it needs the same answer while impersonating
# nobody, and both call the same service so the two can never disagree.
#
# Unauthenticated by necessity - the visitor has no account yet, which is the
# whole point of the form - so they are GET-only, read-only, throttled by IP,
# and expose nothing but times: no meeting, no name, no email.


def _booking_horizon():
    """``(today, last bookable date)`` in UTC, the range both views clamp to."""
    today = timezone.now().date()
    return today, today + timedelta(days=MAX_SLOT_HORIZON_DAYS)


class BookingDaysAPIView(APIView):
    """
    The dates in one month that still have a free slot.

    A month at a time rather than the whole horizon: the calendar renders one
    month, and shipping four months of dates to grey out one would be three
    months of answer nobody reads.

    Returns only the OPEN dates. The absent ones are closed, and a caller
    cannot tell a closed weekday from a blackout from a fully booked day -
    which is correct, because none of those are a visitor's business and all
    three mean the same thing to them.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicBookingThrottle]

    def get(self, request):
        today, horizon = _booking_horizon()

        raw_month = (request.query_params.get('month') or '').strip()
        if raw_month:
            try:
                first = date_type.fromisoformat(f'{raw_month}-01')
            except ValueError:
                return Response(
                    {'detail': 'Ask for a month as YYYY-MM.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            first = today.replace(day=1)

        # The last day of `first`'s month, without a calendar library: step into
        # the next month and back off one day.
        next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        last = next_month - timedelta(days=1)

        # Nothing before today and nothing past the horizon is bookable, so the
        # window is clipped rather than queried and thrown away.
        start = max(first, today)
        end = min(last, horizon)

        days = open_days(start, end) if start <= end else []

        return Response({
            'month': first.strftime('%Y-%m'),
            'min_date': today.isoformat(),
            'max_date': horizon.isoformat(),
            'days': [day.isoformat() for day in days],
        })


class BookingSlotsAPIView(APIView):
    """
    The slots on one date, as the signup form lists them.

    Taken and already-passed slots are returned with ``available: false``
    rather than dropped, so the form can show them struck through - the same
    thing the console does, and the reason a visitor can see that 10:00 exists
    and is gone instead of wondering why the day starts at 10:30.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicBookingThrottle]

    def get(self, request):
        raw_date = (request.query_params.get('date') or '').strip()
        if not raw_date:
            return Response(
                {'detail': 'Name the date to look at.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            target = date_type.fromisoformat(raw_date)
        except ValueError:
            return Response(
                {'detail': 'Ask for a date as YYYY-MM-DD.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        today, horizon = _booking_horizon()
        if target < today or target > horizon:
            # Not an error the form can produce, and not worth a distinct
            # message: a date outside the window has no slots, which is what an
            # empty list already says.
            return Response({'date': target.isoformat(), 'slots': []})

        slots = [
            {
                'time': slot['time'],
                'label': format_slot_label(parse_slot_label(slot['time'])),
                'available': slot['available'],
            }
            for slot in available_slots(target)
        ]

        return Response({'date': target.isoformat(), 'slots': slots})


class ForgotPasswordRequestAPIView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PasswordResetThrottle]

    def post(self, request):
        enforce_csrf(request)
        serializer = ForgotPasswordRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        verification_id = uuid.uuid4()
        user = User.objects.filter(
            email__iexact=serializer.validated_data['email'],
            is_active=True,
        ).first()
        if user is not None:
            verification = create_account_verification(
                user,
                AccountVerification.PASSWORD_RESET,
            )
            verification_id = verification.id

        return Response(
            {
                'detail': (
                    'If an active account exists for this email, '
                    'a verification code has been sent.'
                ),
                'verification_id': verification_id,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class ForgotPasswordConfirmAPIView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PasswordResetThrottle]

    def post(self, request):
        enforce_csrf(request)
        email = str(request.data.get('email', '')).lower()
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        serializer = ForgotPasswordConfirmSerializer(
            data=request.data,
            context={'user': user},
        )
        serializer.is_valid(raise_exception=True)

        if user is None:
            return Response(
                {'otp': ['Invalid or expired verification code.']},
                status=status.HTTP_400_BAD_REQUEST,
            )

        consume_account_verification(
            user,
            serializer.validated_data['verification_id'],
            AccountVerification.PASSWORD_RESET,
            serializer.validated_data['otp'],
        )

        with transaction.atomic():
            user.set_password(serializer.validated_data['new_password'])
            user.save(update_fields=['password'])
            blacklist_user_refresh_tokens(user)

        response = Response(
            {'detail': 'Password reset successfully. Please log in.'}
        )
        clear_auth_cookies(response)
        return response


class CsrfAPIView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    @method_decorator(ensure_csrf_cookie)
    def get(self, request):
        return Response(
            {
                'detail': 'CSRF cookie set.',
                'csrfToken': get_token(request),
            },
            status=status.HTTP_200_OK,
        )


class LoginAPIView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        enforce_csrf(request)
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email']
        password = serializer.validated_data['password']

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response(
                {'detail': 'Invalid email or password.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        authenticated_user = authenticate(
            request=request,
            email=user.email,
            password=password,
        )

        if authenticated_user is None:
            password_is_correct = user.check_password(password)

            if password_is_correct and not user.is_active:
                return Response(
                    {
                        'detail': (
                            'Your account is not active. '
                            'Please contact an administrator.'
                        )
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

            return Response(
                {'detail': 'Invalid email or password.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # Recorded here because `authenticate()` does not fire the signal that
        # normally maintains it, and the admin console reports "last active"
        # from this column.
        update_last_login(None, authenticated_user)

        refresh_token = RefreshToken.for_user(authenticated_user)
        profile_serializer = UserProfileSerializer(authenticated_user)

        response = Response(
            {'user': profile_serializer.data},
            status=status.HTTP_200_OK,
        )
        set_auth_cookies(
            response,
            str(refresh_token.access_token),
            str(refresh_token),
        )
        return response


class RefreshAPIView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        enforce_csrf(request)
        refresh_token = request.COOKIES.get(settings.AUTH_REFRESH_COOKIE_NAME)

        if not refresh_token:
            return Response(
                {'detail': 'Refresh token is missing.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        serializer = TokenRefreshSerializer(data={'refresh': refresh_token})
        serializer.is_valid(raise_exception=True)

        response = Response(status=status.HTTP_204_NO_CONTENT)
        set_auth_cookies(
            response,
            serializer.validated_data['access'],
            serializer.validated_data.get('refresh'),
        )
        return response


class LogoutAPIView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        enforce_csrf(request)
        refresh_token = request.COOKIES.get(settings.AUTH_REFRESH_COOKIE_NAME)

        if refresh_token:
            try:
                RefreshToken(refresh_token).blacklist()
            except TokenError:
                pass

        response = Response(status=status.HTTP_204_NO_CONTENT)
        clear_auth_cookies(response)
        return response


class CurrentUserAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = UserProfileSerializer(request.user)
        return Response(serializer.data, status=status.HTTP_200_OK)
