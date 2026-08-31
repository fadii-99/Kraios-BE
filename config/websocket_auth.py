from http.cookies import SimpleCookie

from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed, TokenError


@database_sync_to_async
def user_from_access_token(raw_token):
    try:
        authentication = JWTAuthentication()
        token = authentication.get_validated_token(raw_token)
        return authentication.get_user(token)
    except (AuthenticationFailed, TokenError):
        return AnonymousUser()


class CookieJWTAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get('headers', []))
        cookie_header = headers.get(b'cookie', b'').decode('latin1')
        cookies = SimpleCookie()
        cookies.load(cookie_header)
        cookie = cookies.get(settings.AUTH_ACCESS_COOKIE_NAME)

        scope['user'] = (
            await user_from_access_token(cookie.value)
            if cookie
            else AnonymousUser()
        )
        return await self.app(scope, receive, send)
