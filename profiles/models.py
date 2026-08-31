import uuid

from django.conf import settings
from django.db import models


class AccountVerification(models.Model):
    PASSWORD_CHANGE = 'PASSWORD_CHANGE'
    PASSWORD_RESET = 'PASSWORD_RESET'
    ACCOUNT_DELETE = 'ACCOUNT_DELETE'

    PURPOSE_CHOICES = [
        (PASSWORD_CHANGE, 'Password change'),
        (PASSWORD_RESET, 'Password reset'),
        (ACCOUNT_DELETE, 'Account deletion'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='account_verifications',
    )
    purpose = models.CharField(max_length=30, choices=PURPOSE_CHOICES)
    otp_hash = models.CharField(max_length=128)
    pending_password_hash = models.CharField(max_length=255, blank=True)
    expires_at = models.DateTimeField()
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'purpose', 'created_at']),
        ]

    def __str__(self):
        return f'{self.user.email} - {self.purpose}'
