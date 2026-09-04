"""
Emails the admin console sends.

Everything goes through ``accounts.emails.send_templated_email`` so the
from-address, the template-id header and the text/HTML pairing are decided in
one place for the whole product.

Every function here is a NON-ESSENTIAL side effect of the operation that calls
it: a reminder that fails to send must not roll back the meeting, and an
undelivered credentials email must not leave the account half-activated. The
callers therefore catch, log, and report delivery separately — see
``services_users.generate_user_password``, which returns whether the message
went out so an administrator can retry rather than assume.

The credentials email is the one place a generated password legitimately
exists in transit. It is never logged, never stored in plaintext, and never
returned in an API response.
"""
import logging

from accounts.emails import EmailTemplateDefinition, send_templated_email


logger = logging.getLogger(__name__)

USER_CREDENTIALS_TEMPLATE_ID = 'admin_user_credentials_v1'
MEETING_REMINDER_USER_TEMPLATE_ID = 'admin_meeting_reminder_user_v1'
MEETING_REMINDER_ADMIN_TEMPLATE_ID = 'admin_meeting_reminder_admin_v1'
MEETING_RESCHEDULED_TEMPLATE_ID = 'admin_meeting_rescheduled_v1'
MEETING_CANCELLED_TEMPLATE_ID = 'admin_meeting_cancelled_v1'
SUBSCRIPTION_ACTIVATED_TEMPLATE_ID = 'admin_subscription_activated_v1'

ADMIN_EMAIL_TEMPLATES = {
    USER_CREDENTIALS_TEMPLATE_ID: EmailTemplateDefinition(
        subject='Your Kraios account is ready',
        text_template='emails/kraios_admin/user_credentials.txt',
        html_template='emails/kraios_admin/user_credentials.html',
    ),
    MEETING_REMINDER_USER_TEMPLATE_ID: EmailTemplateDefinition(
        subject='Reminder: your Kraios walkthrough starts soon',
        text_template='emails/kraios_admin/meeting_reminder_user.txt',
        html_template='emails/kraios_admin/meeting_reminder_user.html',
    ),
    MEETING_REMINDER_ADMIN_TEMPLATE_ID: EmailTemplateDefinition(
        subject='Reminder: a Kraios onboarding call starts soon',
        text_template='emails/kraios_admin/meeting_reminder_admin.txt',
        html_template='emails/kraios_admin/meeting_reminder_admin.html',
    ),
    MEETING_RESCHEDULED_TEMPLATE_ID: EmailTemplateDefinition(
        subject='Your Kraios meeting has been rescheduled',
        text_template='emails/kraios_admin/meeting_rescheduled.txt',
        html_template='emails/kraios_admin/meeting_rescheduled.html',
    ),
    MEETING_CANCELLED_TEMPLATE_ID: EmailTemplateDefinition(
        subject='Your Kraios meeting has been cancelled',
        text_template='emails/kraios_admin/meeting_cancelled.txt',
        html_template='emails/kraios_admin/meeting_cancelled.html',
    ),
    SUBSCRIPTION_ACTIVATED_TEMPLATE_ID: EmailTemplateDefinition(
        subject='Your Kraios plan is active',
        text_template='emails/kraios_admin/subscription_activated.txt',
        html_template='emails/kraios_admin/subscription_activated.html',
    ),
}


def _send(template_id, recipient, context):
    return send_templated_email(
        template_id,
        recipient,
        context,
        templates=ADMIN_EMAIL_TEMPLATES,
    )


def send_user_credentials(user, raw_password, sign_in_url):
    """
    Deliver the generated credentials to the account holder.

    ``raw_password`` reaches this function and nowhere else. Do not add a log
    line here, and do not put it in the audit trail.
    """
    return _send(
        USER_CREDENTIALS_TEMPLATE_ID,
        user.email,
        {
            'full_name': user.full_name,
            'email': user.email,
            'password': raw_password,
            'sign_in_url': sign_in_url,
        },
    )


def send_meeting_reminder_to_user(meeting, minutes_before):
    return _send(
        MEETING_REMINDER_USER_TEMPLATE_ID,
        meeting.user.email,
        {
            'full_name': meeting.user.full_name,
            'scheduled_at': meeting.scheduled_at,
            'minutes_before': minutes_before,
        },
    )


def send_meeting_reminder_to_admin(meeting, recipient, minutes_before):
    return _send(
        MEETING_REMINDER_ADMIN_TEMPLATE_ID,
        recipient,
        {
            'full_name': meeting.user.full_name,
            'firm_name': meeting.user.firm_name,
            'email': meeting.user.email,
            'scheduled_at': meeting.scheduled_at,
            'minutes_before': minutes_before,
        },
    )


def send_meeting_rescheduled(meeting):
    return _send(
        MEETING_RESCHEDULED_TEMPLATE_ID,
        meeting.user.email,
        {
            'full_name': meeting.user.full_name,
            'scheduled_at': meeting.scheduled_at,
        },
    )


def send_meeting_cancelled(meeting):
    return _send(
        MEETING_CANCELLED_TEMPLATE_ID,
        meeting.user.email,
        {'full_name': meeting.user.full_name},
    )


def send_subscription_activated(user, subscription):
    return _send(
        SUBSCRIPTION_ACTIVATED_TEMPLATE_ID,
        user.email,
        {
            'full_name': user.full_name,
            'plan': subscription.get('plan'),
            'renewal_date': subscription.get('renewalDate'),
            'duration_days': subscription.get('durationDays'),
        },
    )
