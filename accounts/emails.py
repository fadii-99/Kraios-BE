from dataclasses import dataclass

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string


SIGNUP_MEETING_CONFIRMATION_TEMPLATE_ID = 'signup_meeting_confirmation_v1'
FORGOT_PASSWORD_OTP_TEMPLATE_ID = 'forgot_password_otp_v1'
DELETE_ACCOUNT_OTP_TEMPLATE_ID = 'delete_account_otp_v1'
CHANGE_PASSWORD_OTP_TEMPLATE_ID = 'change_password_otp_v1'


@dataclass(frozen=True)
class EmailTemplateDefinition:
    subject: str
    text_template: str
    html_template: str


EMAIL_TEMPLATES = {
    SIGNUP_MEETING_CONFIRMATION_TEMPLATE_ID: EmailTemplateDefinition(
        subject='Your Kraios meeting is scheduled',
        text_template='emails/signup_meeting_confirmation.txt',
        html_template='emails/signup_meeting_confirmation.html',
    ),
    FORGOT_PASSWORD_OTP_TEMPLATE_ID: EmailTemplateDefinition(
        subject='Reset your Kraios password',
        text_template='emails/forgot_password_otp.txt',
        html_template='emails/forgot_password_otp.html',
    ),
    DELETE_ACCOUNT_OTP_TEMPLATE_ID: EmailTemplateDefinition(
        subject='Confirm your Kraios account deletion',
        text_template='emails/delete_account_otp.txt',
        html_template='emails/delete_account_otp.html',
    ),
    CHANGE_PASSWORD_OTP_TEMPLATE_ID: EmailTemplateDefinition(
        subject='Confirm your Kraios password change',
        text_template='emails/change_password_otp.txt',
        html_template='emails/change_password_otp.html',
    ),
}


def send_templated_email(template_id, recipient, context):
    template = EMAIL_TEMPLATES[template_id]
    complete_context = {
        **context,
        'app_name': 'Kraios',
        'support_email': settings.KRAIOS_SUPPORT_EMAIL,
    }
    text_body = render_to_string(template.text_template, complete_context).strip()
    html_body = render_to_string(template.html_template, complete_context)

    message = EmailMultiAlternatives(
        subject=template.subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[recipient],
        headers={'X-Kraios-Template-ID': template_id},
    )
    message.attach_alternative(html_body, 'text/html')
    return message.send(fail_silently=False)


def send_signup_meeting_confirmation(signup_request):
    user = signup_request.user
    return send_templated_email(
        SIGNUP_MEETING_CONFIRMATION_TEMPLATE_ID,
        user.email,
        {
            'full_name': user.full_name,
            'preferred_date': signup_request.preferred_date,
            'preferred_time': signup_request.preferred_time,
        },
    )


def send_otp_email(user, template_id, otp, expiry_minutes):
    return send_templated_email(
        template_id,
        user.email,
        {
            'full_name': user.full_name,
            'otp': otp,
            'expiry_minutes': expiry_minutes,
        },
    )

