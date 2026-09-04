"""
Admin sign-in: lockout, credential checking, session issue and revocation.

Two independent brakes sit in front of the credential check, because they stop
different attacks:

* The DRF throttle (``throttles.AdminLoginThrottle``) caps the request *rate*
  from one ``(email, IP)`` pair. It lives in the cache and is therefore fast,
  but a cache flush clears it.
* The lockout below counts *failures* in the database over a window. It
  survives a cache flush and a process restart, which is what makes it the
  authority. It locks the ``(email, IP)`` pair after a handful of misses and
  the IP alone after many — never the email alone, because that would let
  anybody lock a real administrator out of their own console by guessing at
  their address.

Both are advisory to the caller in the same way: a neutral 429 with
``Retry-After`` and no statement about whether the address exists.
"""
import logging
import secrets
from datetime import timedelta

from django.contrib.auth import authenticate
from django.contrib.auth.models import update_last_login
from django.db import transaction
from django.utils import timezone

from profiles.services import blacklist_user_refresh_tokens

from .audit import client_ip, record_admin_action
from .models import AdminLoginAttempt, AdminProfile
from .tokens import issue_admin_tokens


logger = logging.getLogger(__name__)

# How far back a failure still counts against the caller.
FAILURE_WINDOW = timedelta(minutes=15)

# Misses tolerated from one (email, IP) pair inside the window. Five is enough
# for a person mistyping a password twice and a stale saved credential once.
MAX_FAILURES_PER_EMAIL_IP = 5

# Misses tolerated from one IP across all addresses. Higher, because a shared
# office NAT legitimately produces several administrators' attempts.
MAX_FAILURES_PER_IP = 20

# How long a tripped lockout holds. Deliberately equal to the window, so the
# lock lifts exactly when the offending attempts age out.
LOCKOUT_SECONDS = int(FAILURE_WINDOW.total_seconds())

GENERIC_LOGIN_FAILURE = 'Incorrect email or password.'


class AdminLoginError(Exception):
    """A sign-in attempt was refused. Carries only caller-safe text."""

    http_status = 401

    def __init__(self, message=GENERIC_LOGIN_FAILURE):
        super().__init__(message)
        self.message = message


class AdminLoginLocked(AdminLoginError):
    http_status = 429

    def __init__(self, retry_after=LOCKOUT_SECONDS):
        super().__init__(
            'Too many sign-in attempts. Try again in a few minutes.'
        )
        self.retry_after = retry_after


def _recent_failures(*, email=None, ip_address=None):
    since = timezone.now() - FAILURE_WINDOW
    queryset = AdminLoginAttempt.objects.filter(succeeded=False, created_at__gte=since)

    if email is not None:
        queryset = queryset.filter(email=email)
    if ip_address is not None:
        queryset = queryset.filter(ip_address=ip_address)

    return queryset.count()


def assert_login_not_locked(email, ip_address):
    """Raise ``AdminLoginLocked`` when this caller has spent its attempts."""
    if ip_address is not None:
        if _recent_failures(email=email, ip_address=ip_address) >= MAX_FAILURES_PER_EMAIL_IP:
            logger.warning('Admin login locked for %s from %s.', email, ip_address)
            raise AdminLoginLocked()

        if _recent_failures(ip_address=ip_address) >= MAX_FAILURES_PER_IP:
            logger.warning('Admin login locked for host %s.', ip_address)
            raise AdminLoginLocked()

    elif _recent_failures(email=email) >= MAX_FAILURES_PER_EMAIL_IP:
        # No usable client address (direct socket, misconfigured proxy). Fall
        # back to the email alone rather than leaving the endpoint unlimited.
        raise AdminLoginLocked()


def _record_attempt(email, ip_address, succeeded):
    try:
        AdminLoginAttempt.objects.create(
            email=email,
            ip_address=ip_address,
            succeeded=succeeded,
        )
    except Exception:
        # The ledger is a control, not the operation. Losing one row must not
        # turn a correct sign-in into a 500.
        logger.exception('Could not record an admin login attempt for %s.', email)


def sign_in_admin(request, email, password):
    """
    Verify admin credentials and issue a session.

    Returns ``(admin_profile, access_token, refresh_token)``.
    Raises ``AdminLoginError`` (or ``AdminLoginLocked``) on any refusal, with
    one message for every cause.
    """
    email = str(email or '').strip().lower()[:254]
    ip_address = client_ip(request)

    assert_login_not_locked(email, ip_address)

    # `authenticate` runs the password hasher even when no user matches, so a
    # missing address and a wrong password cost the same wall-clock time.
    authenticated_user = authenticate(request=request, email=email, password=password)

    admin_profile = None
    if authenticated_user is not None:
        admin_profile = (
            AdminProfile.objects.select_related('user')
            .filter(user=authenticated_user)
            .first()
        )

    if admin_profile is None or not admin_profile.is_active:
        _record_attempt(email, ip_address, succeeded=False)
        if authenticated_user is not None:
            # Correct password, but not an administrator. Worth a log line:
            # it is either a misconfiguration or a customer probing the console.
            logger.warning(
                'Valid non-admin credentials presented at the admin console (user %s).',
                authenticated_user.pk,
            )
        raise AdminLoginError()

    _record_attempt(email, ip_address, succeeded=True)

    now = timezone.now()
    admin_profile.last_login_at = now
    admin_profile.last_login_ip = ip_address
    admin_profile.save(update_fields=['last_login_at', 'last_login_ip', 'updated_at'])
    update_last_login(None, admin_profile.user)

    access_token, refresh_token = issue_admin_tokens(admin_profile)

    record_admin_action(
        admin_profile,
        'admin.sign_in',
        target_type='admin',
        target_id=admin_profile.pk,
        summary='Signed in to the admin console',
        request=request,
    )

    return admin_profile, access_token, refresh_token


def sign_out_admin(admin_profile, request=None):
    """
    End every session this administrator holds.

    Rotating the session secret is what makes the *access* token die too. The
    refresh token is blacklisted as well, so the pair cannot be replayed even
    if the secret were somehow restored.
    """
    with transaction.atomic():
        blacklist_user_refresh_tokens(admin_profile.user)
        admin_profile.rotate_session_secret()

    record_admin_action(
        admin_profile,
        'admin.sign_out',
        target_type='admin',
        target_id=admin_profile.pk,
        summary='Signed out of the admin console',
        request=request,
    )


def change_admin_password(admin_profile, current_password, new_password, request=None):
    """
    Change the signed-in administrator's own password.

    Every existing session is destroyed, including the one making the request:
    a password change that leaves the old sessions alive is not a password
    change, it is a rename.
    """
    if not admin_profile.user.check_password(current_password):
        raise AdminLoginError('Your current password is incorrect.')

    with transaction.atomic():
        admin_profile.user.set_password(new_password)
        admin_profile.user.save(update_fields=['password'])
        blacklist_user_refresh_tokens(admin_profile.user)
        admin_profile.rotate_session_secret()

    record_admin_action(
        admin_profile,
        'admin.password_change',
        target_type='admin',
        target_id=admin_profile.pk,
        summary='Changed their own admin password',
        request=request,
    )


def resolve_admin_for_refresh(user_id, session_secret):
    """
    The profile a refresh token belongs to, or ``None`` when it is no longer
    entitled to one.

    Every condition is re-read here rather than trusted from the token, which
    is the point: a refresh presented one minute after a deactivation must not
    mint a new access token.
    """
    admin_profile = (
        AdminProfile.objects.select_related('user')
        .filter(user_id=user_id, is_active=True)
        .first()
    )

    if admin_profile is None or not admin_profile.user.is_active:
        return None

    if not secrets.compare_digest(str(session_secret or ''), admin_profile.session_secret):
        return None

    return admin_profile
