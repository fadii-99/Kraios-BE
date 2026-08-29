from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .forms import AdminUserChangeForm, AdminUserCreationForm
from .models import SignupRequest, User


@admin.register(User)
class AccountUserAdmin(UserAdmin):
    add_form = AdminUserCreationForm
    form = AdminUserChangeForm
    model = User

    list_display = ('email', 'full_name', 'firm_name', 'role', 'is_active', 'is_staff')
    list_filter = ('is_active', 'is_staff', 'role', 'country')
    search_fields = ('email', 'full_name', 'firm_name')
    ordering = ('email',)

    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        (
            'Profile',
            {
                'fields': (
                    'full_name',
                    'firm_name',
                    'country',
                    'job_title',
                    'phone',
                    'role',
                )
            },
        ),
        (
            'Permissions',
            {
                'fields': (
                    'is_active',
                    'is_staff',
                    'is_superuser',
                    'groups',
                    'user_permissions',
                )
            },
        ),
        ('Important dates', {'fields': ('last_login', 'date_joined')}),
    )

    add_fieldsets = (
        (
            None,
            {
                'classes': ('wide',),
                'fields': (
                    'email',
                    'full_name',
                    'firm_name',
                    'country',
                    'password1',
                    'password2',
                    'is_active',
                    'is_staff',
                ),
            },
        ),
    )


@admin.register(SignupRequest)
class SignupRequestAdmin(admin.ModelAdmin):
    list_display = (
        'email',
        'full_name',
        'firm_name',
        'preferred_date',
        'preferred_time',
        'status',
        'created_at',
    )
    list_filter = ('status', 'preferred_date', 'user__country')
    list_editable = ('status',)
    search_fields = ('user__email', 'user__full_name', 'user__firm_name')
    readonly_fields = ('user', 'created_at', 'updated_at')

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset.select_related('user')

    def has_add_permission(self, request):
        return False

    @admin.display(description='Email', ordering='user__email')
    def email(self, signup_request):
        return signup_request.user.email

    @admin.display(description='Name', ordering='user__full_name')
    def full_name(self, signup_request):
        return signup_request.user.full_name

    @admin.display(description='Firm', ordering='user__firm_name')
    def firm_name(self, signup_request):
        return signup_request.user.firm_name
