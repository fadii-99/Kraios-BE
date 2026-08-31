from django.contrib import admin

from .models import AccountVerification


@admin.register(AccountVerification)
class AccountVerificationAdmin(admin.ModelAdmin):
    list_display = ('user', 'purpose', 'created_at', 'expires_at', 'consumed_at')
    list_filter = ('purpose', 'consumed_at')
    search_fields = ('user__email',)
    readonly_fields = (
        'user',
        'purpose',
        'otp_hash',
        'pending_password_hash',
        'expires_at',
        'failed_attempts',
        'consumed_at',
        'created_at',
    )

    def has_add_permission(self, request):
        return False

