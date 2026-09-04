"""
Authorization for the admin console.

Two levels only, and the split is deliberate: everything an administrator does
day to day (users, meetings, availability, usage, support) needs
``IsKraiosAdmin``; the operations that change who can administer the platform,
what the plan catalogue contains, or that read the audit trail need
``IsPrivilegedAdmin``. A single "is logged in" check would make the audit log
readable by every support operator, which defeats the point of having one.
"""
from rest_framework.permissions import BasePermission

from .models import AdminProfile


def resolve_admin_profile(request):
    """
    The ``AdminProfile`` for the authenticated caller, or ``None``.

    ``AdminCookieJWTAuthentication`` caches it on the request; the database
    fallback exists so a view is still correct if it is ever reached through a
    different authentication class rather than silently treating the caller as
    unprivileged.
    """
    cached = getattr(request, 'admin_profile', None)
    if cached is not None:
        return cached

    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return None

    profile = (
        AdminProfile.objects.select_related('user')
        .filter(user=user, is_active=True)
        .first()
    )
    request.admin_profile = profile
    return profile


class IsKraiosAdmin(BasePermission):
    """The caller holds an active administrator profile."""

    message = 'An active KRAIOS admin account is required.'

    def has_permission(self, request, view):
        profile = resolve_admin_profile(request)
        return bool(profile and profile.is_active and profile.user.is_active)


class IsPrivilegedAdmin(IsKraiosAdmin):
    """The caller is a Super Admin."""

    message = 'This action requires a Super Admin account.'

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        return resolve_admin_profile(request).is_privileged
