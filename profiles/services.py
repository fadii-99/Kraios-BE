import secrets
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)

from accounts.emails import (
    CHANGE_PASSWORD_OTP_TEMPLATE_ID,
    DELETE_ACCOUNT_OTP_TEMPLATE_ID,
    FORGOT_PASSWORD_OTP_TEMPLATE_ID,
    send_otp_email,
)
from .models import AccountVerification


OTP_EXPIRY_MINUTES = 10
OTP_MAX_ATTEMPTS = 5

OTP_EMAIL_TEMPLATES = {
    AccountVerification.PASSWORD_CHANGE: CHANGE_PASSWORD_OTP_TEMPLATE_ID,
    AccountVerification.PASSWORD_RESET: FORGOT_PASSWORD_OTP_TEMPLATE_ID,
    AccountVerification.ACCOUNT_DELETE: DELETE_ACCOUNT_OTP_TEMPLATE_ID,
}


def blacklist_user_refresh_tokens(user):
    for token in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=token)


def create_account_verification(user, purpose, pending_password=''):
    now = timezone.now()
    AccountVerification.objects.filter(
        user=user,
        purpose=purpose,
        consumed_at__isnull=True,
    ).update(consumed_at=now)

    otp = f'{secrets.randbelow(1_000_000):06d}'
    verification = AccountVerification.objects.create(
        user=user,
        purpose=purpose,
        otp_hash=make_password(otp),
        pending_password_hash=(
            make_password(pending_password) if pending_password else ''
        ),
        expires_at=now + timedelta(minutes=OTP_EXPIRY_MINUTES),
    )

    send_otp_email(
        user=user,
        template_id=OTP_EMAIL_TEMPLATES[purpose],
        otp=otp,
        expiry_minutes=OTP_EXPIRY_MINUTES,
    )
    return verification


def consume_account_verification(user, verification_id, purpose, otp):
    invalid_otp = False

    with transaction.atomic():
        try:
            verification = AccountVerification.objects.select_for_update().get(
                id=verification_id,
                user=user,
                purpose=purpose,
            )
        except AccountVerification.DoesNotExist as exc:
            raise ValidationError({'otp': 'Invalid verification request.'}) from exc

        if verification.consumed_at is not None:
            raise ValidationError(
                {'otp': 'This verification code has already been used.'}
            )

        if verification.expires_at <= timezone.now():
            raise ValidationError({'otp': 'This verification code has expired.'})

        if verification.failed_attempts >= OTP_MAX_ATTEMPTS:
            raise ValidationError(
                {'otp': 'Too many incorrect attempts. Request a new code.'}
            )

        if not check_password(otp, verification.otp_hash):
            verification.failed_attempts += 1
            verification.save(update_fields=['failed_attempts'])
            invalid_otp = True
        else:
            verification.consumed_at = timezone.now()
            verification.save(update_fields=['consumed_at'])

    if invalid_otp:
        raise ValidationError({'otp': 'The verification code is incorrect.'})

    return verification
