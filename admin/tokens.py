"""
Issuing, rotating and transporting the admin console's session.

The console is a browser application on a different origin from the API, so the
session is carried in ``HttpOnly`` cookies rather than in a header a script can
read — the same decision, and the same CSRF obligation, as the customer API in
``accounts``. Three things make the admin session distinct from a customer one,
and all three matter:

1. **Different cookie names.** A customer's access cookie is never presented to
   an admin endpoint and vice versa, so a stolen customer session cannot be
   replayed here even if both applications share a hostname.
2. **A scope claim.** A token without ``scope == ADMIN_TOKEN_SCOPE`` is refused
   by ``AdminCookieJWTAuthentication``. The customer login endpoint cannot mint
   one, so a customer token is not merely inconvenient to present here — it is
   invalid.
3. **A session-binding claim.** Every token embeds the administrator's
   ``session_secret``. Rotating that column (logout, password change,
   deactivation) invalidates every outstanding token for that administrator
   immediately, instead of leaving a valid access token alive until it expires.

The role is also embedded, but ONLY as a debugging aid: privilege is re-read
from the database on every request, because a token issued to a Super Admin
must not still act as one after the role has been reduced.
"""
import logging

from django.conf import settings
from rest_framework_simplejwt.tokens import RefreshToken


logger = logging.getLogger(__name__)

ADMIN_TOKEN_SCOPE = 'kraios-admin'

SCOPE_CLAIM = 'scope'
SESSION_CLAIM = 'sid'
ROLE_CLAIM = 'role'


def issue_admin_tokens(admin_profile):
    """
    Mint an access/refresh pair for one administrator.

    Returns ``(access_token, refresh_token)`` as encoded strings.
    """
    refresh = RefreshToken.for_user(admin_profile.user)
    refresh[SCOPE_CLAIM] = ADMIN_TOKEN_SCOPE
    refresh[SESSION_CLAIM] = admin_profile.session_secret
    refresh[ROLE_CLAIM] = admin_profile.role

    # The admin session is deliberately shorter-lived than the customer one:
    # this credential can deactivate accounts and issue passwords. `set_exp`
    # runs after `for_user`, so the bookkeeping row the blacklist app wrote
    # keeps the library's default expiry — harmless, because revocation is
    # matched on `jti`, never on that column.
    refresh.set_exp(lifetime=settings.ADMIN_REFRESH_TOKEN_LIFETIME)

    access = refresh.access_token
    access.set_exp(lifetime=settings.ADMIN_ACCESS_TOKEN_LIFETIME)

    return str(access), str(refresh)


def set_admin_auth_cookies(response, access_token, refresh_token=None):
    response.set_cookie(
        settings.ADMIN_AUTH_ACCESS_COOKIE_NAME,
        access_token,
        max_age=int(settings.ADMIN_ACCESS_TOKEN_LIFETIME.total_seconds()),
        httponly=True,
        secure=settings.ADMIN_AUTH_COOKIE_SECURE,
        samesite=settings.ADMIN_AUTH_COOKIE_SAMESITE,
        path='/',
    )

    if refresh_token is not None:
        response.set_cookie(
            settings.ADMIN_AUTH_REFRESH_COOKIE_NAME,
            refresh_token,
            max_age=int(settings.ADMIN_REFRESH_TOKEN_LIFETIME.total_seconds()),
            httponly=True,
            secure=settings.ADMIN_AUTH_COOKIE_SECURE,
            samesite=settings.ADMIN_AUTH_COOKIE_SAMESITE,
            path='/',
        )


def clear_admin_auth_cookies(response):
    response.delete_cookie(
        settings.ADMIN_AUTH_ACCESS_COOKIE_NAME,
        path='/',
        samesite=settings.ADMIN_AUTH_COOKIE_SAMESITE,
    )
    response.delete_cookie(
        settings.ADMIN_AUTH_REFRESH_COOKIE_NAME,
        path='/',
        samesite=settings.ADMIN_AUTH_COOKIE_SAMESITE,
    )
