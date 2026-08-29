from django.conf import settings
from rest_framework.authentication import CSRFCheck
from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.authentication import JWTAuthentication


class CookieJWTAuthentication(JWTAuthentication):
    """Authenticate requests with the HttpOnly access-token cookie."""

    def authenticate(self, request):
        raw_token = request.COOKIES.get(settings.AUTH_ACCESS_COOKIE_NAME)

        if not raw_token:
            return None

        validated_token = self.get_validated_token(raw_token)
        self.enforce_csrf(request)
        return self.get_user(validated_token), validated_token

    def enforce_csrf(self, request):
        csrf_check = CSRFCheck(lambda request: None)
        csrf_check.process_request(request)
        reason = csrf_check.process_view(request, None, (), {})

        if reason:
            raise PermissionDenied(f'CSRF Failed: {reason}')
