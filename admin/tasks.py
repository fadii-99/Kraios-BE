"""
Scheduled work for the admin console.

MEETING REMINDERS. The brief is that the customer must get a reminder before
the call, roughly an hour ahead, and that the administrators are told too. The
task runs on a short interval and sends for every meeting whose start falls
inside the lead window.

IDEMPOTENCY IS THE WHOLE DESIGN. Two workers run in ``compose.yaml``, the beat
schedule can fire while a previous run is still going, and Celery will redeliver
a task whose worker died. So a meeting is CLAIMED before anything is sent, with
a single conditional UPDATE:

    Meeting.objects.filter(pk=..., user_reminder_sent_at__isnull=True)
                   .update(user_reminder_sent_at=now)

The database decides the winner. Exactly one caller sees ``1`` returned and
sends; every other caller sees ``0`` and moves on. Claiming BEFORE sending
means a crash between the claim and the send loses one reminder; claiming after
would mean a crash sends the same reminder on every retry forever. A lost
reminder is recoverable and an email loop is not, so the order is deliberate.

The window has a tail (``grace``) so a worker that was down for a few minutes
still delivers, and it never reaches past the meeting's own start time - a
"starts in 5 minutes" email for a call that began twenty minutes ago is worse
than silence.
"""
import logging

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .emails import send_meeting_reminder_to_admin, send_meeting_reminder_to_user
from .models import AdminProfile, Meeting, prune_expired_login_attempts
from .services_auth import FAILURE_WINDOW
from .services_meetings import due_reminders


logger = logging.getLogger(__name__)

# Never notify more administrators than this in one go. A misconfiguration that
# marked fifty accounts as administrators should not turn every booking into
# fifty emails.
MAX_ADMIN_RECIPIENTS = 10

# Login attempts are kept for twice the lockout window, which is long enough to
# investigate a burst and short enough that the table stays small.
LOGIN_ATTEMPT_RETENTION = FAILURE_WINDOW * 2


def _admin_recipients():
    """Addresses to alert about an upcoming call."""
    configured = getattr(settings, 'KRAIOS_ADMIN_ALERT_EMAILS', None)
    if configured:
        return list(configured)[:MAX_ADMIN_RECIPIENTS]

    return list(
        AdminProfile.objects.filter(is_active=True, user__is_active=True)
        .values_list('user__email', flat=True)[:MAX_ADMIN_RECIPIENTS]
    )


@shared_task(name='admin.tasks.send_due_meeting_reminders')
def send_due_meeting_reminders():
    """Send the pre-meeting reminders that have come due. Returns a count."""
    lead = settings.KRAIOS_ADMIN_REMINDER_LEAD_MINUTES
    grace = settings.KRAIOS_ADMIN_REMINDER_GRACE_MINUTES

    recipients = _admin_recipients()
    sent = 0

    for meeting in due_reminders(lead, grace):
        now = timezone.now()

        claimed = Meeting.objects.filter(
            pk=meeting.pk,
            user_reminder_sent_at__isnull=True,
        ).update(user_reminder_sent_at=now)

        if not claimed:
            # Another worker got there first.
            continue

        minutes_before = max(1, round((meeting.scheduled_at - now).total_seconds() / 60))

        try:
            send_meeting_reminder_to_user(meeting, minutes_before)
            sent += 1
        except Exception:
            logger.exception(
                'Could not send the customer reminder for meeting %s.', meeting.pk
            )

        admin_claimed = Meeting.objects.filter(
            pk=meeting.pk,
            admin_reminder_sent_at__isnull=True,
        ).update(admin_reminder_sent_at=now)

        if not admin_claimed:
            continue

        for recipient in recipients:
            try:
                send_meeting_reminder_to_admin(meeting, recipient, minutes_before)
            except Exception:
                logger.exception(
                    'Could not send the admin reminder for meeting %s.', meeting.pk
                )

    if sent:
        logger.info('Sent %s meeting reminder(s).', sent)
    return sent


@shared_task(name='admin.tasks.prune_login_attempts')
def prune_login_attempts():
    """Drop login-attempt rows that can no longer affect a lockout decision."""
    deleted = prune_expired_login_attempts(LOGIN_ATTEMPT_RETENTION)
    if deleted:
        logger.info('Pruned %s expired admin login attempt(s).', deleted)
    return deleted
