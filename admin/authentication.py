"""
Authentication for the admin console API.

Every fact this class needs in order to say "yes" is re-read from the database.
The token supplies an identity and a session binding, nothing else: role,
active status and the very existence of an administrator profile are looked up
on each request, so a privilege removed a second ago is gone a second ago.

Failures are reported to the caller as one generic message. Which of the five
checks below failed is a fact only the server needs, and telling a caller "that
token is fine but your profile is suspended" is a free reconnaissance step.
"""
import logging
import secrets

from django.conf import settings
from rest_framework.authentication import CSRFCheck
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework_simplejwt.authentication import JWTAuthentication

from .models import AdminProfile
from .tokens import ADMIN_TOKEN_SCOPE, SCOPE_CLAIM, SESSION_CLAIM


logger = logging.getLogger(__name__)

INVALID_SESSION_MESSAGE = 'Your admin session is no longer valid. Please sign in again.'


class AdminCookieJWTAuthentication(JWTAuthentication):
    """Authenticate an administrator from the admin access-token cookie."""

    def authenticate(self, request):
        raw_token = request.COOKIES.get(settings.ADMIN_AUTH_ACCESS_COOKIE_NAME)

        if not raw_token:
            # No credential at all is "anonymous", not "denied" — the
            # permission class turns that into the 401 the console expects.
            return None

        validated_token = self.get_validated_token(raw_token)

        if validated_token.get(SCOPE_CLAIM) != ADMIN_TOKEN_SCOPE:
            logger.info('Admin endpoint reached with a non-admin token.')
            raise AuthenticationFailed(INVALID_SESSION_MESSAGE)

        user = self.get_user(validated_token)

        admin_profile = (
            AdminProfile.objects.select_related('user')
            .filter(user=user)
            .first()
        )

        if admin_profile is None or not admin_profile.is_active or not user.is_active:
            logger.info(
                'Admin token presented for a user without an active admin profile (user %s).',
                user.pk,
            )
            raise AuthenticationFailed(INVALID_SESSION_MESSAGE)

        presented_session = str(validated_token.get(SESSION_CLAIM) or '')
        if not secrets.compare_digest(presented_session, admin_profile.session_secret):
            logger.info('Revoked admin session presented (admin %s).', admin_profile.pk)
            raise AuthenticationFailed(INVALID_SESSION_MESSAGE)

        self.enforce_csrf(request)

        # Cached so views and services do not re-query for the caller.
        request.admin_profile = admin_profile
        return admin_profile.user, validated_token

    def enforce_csrf(self, request):
        """
        Cookie credentials are sent by the browser automatically, so every
        request carrying one must also prove it came from our own page.
        """
        csrf_check = CSRFCheck(lambda inner_request: None)
        csrf_check.process_request(request)
        reason = csrf_check.process_view(request, None, (), {})

        if reason:
            raise PermissionDenied(f'CSRF Failed: {reason}')
