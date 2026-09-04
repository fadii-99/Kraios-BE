"""
The administrative audit trail.

Every write an administrator makes goes through ``record_admin_action``. It is
called explicitly from the services rather than from a model signal so that the
call site — and therefore the reason the entry exists — is greppable, and so a
failure to write the trail can never roll back the operation it describes: an
audit entry is a record OF the change, not a precondition FOR it.

``metadata`` is for the small facts that make an entry answerable later (old
value, new value, duration). It must never carry a password, a token, or a full
request body.
"""
import logging

from .models import AdminAuditLog


logger = logging.getLogger(__name__)

# Keys that must never reach the audit trail even if a caller passes them.
_FORBIDDEN_METADATA_KEYS = frozenset({
    'password',
    'new_password',
    'raw_password',
    'token',
    'secret',
    'otp',
})


def client_ip(request):
    """Best-effort client address, taking the first hop of X-Forwarded-For."""
    if request is None:
        return None

    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip() or None

    return request.META.get('REMOTE_ADDR') or None


def record_admin_action(
    admin_profile,
    action,
    *,
    target_type='',
    target_id='',
    summary='',
    metadata=None,
    request=None,
):
    """Append one entry to the audit trail. Never raises."""
    safe_metadata = {
        key: value
        for key, value in (metadata or {}).items()
        if key.lower() not in _FORBIDDEN_METADATA_KEYS
    }

    try:
        return AdminAuditLog.objects.create(
            admin=admin_profile,
            admin_email=getattr(getattr(admin_profile, 'user', None), 'email', '') or 'system',
            action=action,
            target_type=target_type,
            target_id=str(target_id or '')[:64],
            summary=str(summary or '')[:255],
            metadata=safe_metadata,
            ip_address=client_ip(request),
        )
    except Exception:
        logger.exception('Could not write audit entry for action %s.', action)
        return None
