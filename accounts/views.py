from django.conf import settings
from django.contrib.auth import authenticate
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
from django.contrib.auth.hashers import make_password
from .models import User
from .serializers import LoginSerializer, SignupRequestSerializer, UserProfileSerializer


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

        response_data = {
            'message': 'Sign-up request submitted successfully.',
            'request_id': signup_request.id,
            'status': signup_request.status,
        }
        return Response(response_data, status=status.HTTP_201_CREATED)


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
        print(email)

        try:
            user = User.objects.get(email=email)
            # user.password=make_password('123456789')
            # user.is_active=True
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
