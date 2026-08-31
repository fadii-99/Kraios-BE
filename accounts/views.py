import logging
import uuid

from django.conf import settings
from django.contrib.auth import authenticate
from django.db import transaction
from django.middleware.csrf import get_token
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
        from django.contrib.auth.hashers import make_password
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email']
        password = serializer.validated_data['password']

        try:
            user = User.objects.get(email=email)
            # user.password = make_password("123456789")
            # user.is_active = True
            # user.save()
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
