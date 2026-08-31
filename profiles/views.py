from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AccountVerification
from .serializers import (
    DeleteAccountRequestSerializer,
    OTPConfirmSerializer,
    PasswordChangeRequestSerializer,
    ProfileSerializer,
)
from .services import (
    blacklist_user_refresh_tokens,
    create_account_verification,
    consume_account_verification,
)
from .throttles import ProfileOTPThrottle


class ProfileAPIView(APIView):
    def get(self, request):
        return Response(ProfileSerializer(request.user).data)

    def patch(self, request):
        serializer = ProfileSerializer(
            request.user,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class PasswordChangeRequestAPIView(APIView):
    throttle_classes = [ProfileOTPThrottle]

    def post(self, request):
        serializer = PasswordChangeRequestSerializer(
            data=request.data,
            context={'request': request},
        )
        serializer.is_valid(raise_exception=True)
        verification = create_account_verification(
            request.user,
            AccountVerification.PASSWORD_CHANGE,
            pending_password=serializer.validated_data['new_password'],
        )
        return Response(
            {
                'detail': 'A verification code has been sent to your email.',
                'verification_id': verification.id,
                'expires_at': verification.expires_at,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class PasswordChangeConfirmAPIView(APIView):
    def post(self, request):
        serializer = OTPConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        verification = consume_account_verification(
            request.user,
            serializer.validated_data['verification_id'],
            AccountVerification.PASSWORD_CHANGE,
            serializer.validated_data['otp'],
        )

        request.user.password = verification.pending_password_hash
        request.user.save(update_fields=['password'])
        blacklist_user_refresh_tokens(request.user)

        response = Response(
            {'detail': 'Password changed successfully. Please log in again.'}
        )
        response.delete_cookie(settings.AUTH_ACCESS_COOKIE_NAME, path='/')
        response.delete_cookie(settings.AUTH_REFRESH_COOKIE_NAME, path='/')
        return response


class DeleteAccountRequestAPIView(APIView):
    throttle_classes = [ProfileOTPThrottle]

    def post(self, request):
        serializer = DeleteAccountRequestSerializer(
            data=request.data,
            context={'request': request},
        )
        serializer.is_valid(raise_exception=True)
        verification = create_account_verification(
            request.user,
            AccountVerification.ACCOUNT_DELETE,
        )
        return Response(
            {
                'detail': 'A verification code has been sent to your email.',
                'verification_id': verification.id,
                'expires_at': verification.expires_at,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class DeleteAccountConfirmAPIView(APIView):
    def post(self, request):
        serializer = OTPConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        consume_account_verification(
            request.user,
            serializer.validated_data['verification_id'],
            AccountVerification.ACCOUNT_DELETE,
            serializer.validated_data['otp'],
        )

        from projects.services import delete_project_files

        for project in request.user.projects.all():
            delete_project_files(project)
        request.user.delete()
        response = Response(status=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(settings.AUTH_ACCESS_COOKIE_NAME, path='/')
        response.delete_cookie(settings.AUTH_REFRESH_COOKIE_NAME, path='/')
        return response
