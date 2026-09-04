"""
Records owned by the KRAIOS admin console.

Scope of this module, and the reasoning behind it:

* ``AdminProfile`` is what makes a ``User`` an administrator. It is a separate
  row rather than a flag on ``accounts.User`` so that administrator privilege
  can never be granted by the public signup path — that path writes ``User``
  and nothing else, and no field it can reach appears here. It also carries
  ``session_secret``, which is what makes revocation immediate: every admin
  token embeds the secret, and rotating it kills every outstanding token for
  that administrator without waiting for an expiry.
* ``AdminLoginAttempt`` is the ledger the lockout in ``services_auth`` reads.
  It is a table rather than a cache counter because a lockout that a cache
  flush silently clears is not a lockout.
* ``AdminAuditLog`` records every administrative write. ``admin_email`` is
  denormalised on purpose: an audit trail that loses the actor when the actor's
  row is deleted is not an audit trail.
* ``Meeting`` is the onboarding call. It is a new model rather than an
  extension of ``accounts.SignupRequest`` because a signup request is
  one-per-user by construction (``OneToOneField``) while an account routinely
  holds several meetings — a cancellation followed by a rebooking is the normal
  case, and the console reports the most recent one.
* ``AvailabilityRule`` / ``AvailabilityBlackout`` are the administrator's own
  bookable hours, from which free slots are computed.

Subscription plans and per-user subscriptions are deliberately NOT modelled
here. There is no payment gateway yet and the plan catalogue is not settled, so
that data lives in the file-backed dummy store in ``dummy_data.py`` and no
migration commits it to the database. See that module.
"""
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


def generate_session_secret():
    """A fresh binding secret for an administrator's tokens."""
    return secrets.token_urlsafe(32)


class AdminProfile(models.Model):
    SUPER_ADMIN = 'SUPER_ADMIN'
    ADMIN = 'ADMIN'
    SUPPORT_ADMIN = 'SUPPORT_ADMIN'

    ROLE_CHOICES = [
        (SUPER_ADMIN, 'Super Admin'),
        (ADMIN, 'Admin'),
        (SUPPORT_ADMIN, 'Support Admin'),
    ]

    # Roles that may change another administrator's account, read the audit
    # trail, or manage the plan catalogue. Everything else is available to any
    # active administrator.
    PRIVILEGED_ROLES = frozenset({SUPER_ADMIN})

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='admin_profile',
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ADMIN)
    is_active = models.BooleanField(default=True)
    # Embedded in every admin token. Rotating it invalidates all of them at
    # once — see `authentication.AdminCookieJWTAuthentication`.
    session_secret = models.CharField(
        max_length=64,
        default=generate_session_secret,
    )
    last_login_at = models.DateTimeField(null=True, blank=True)
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['user__full_name']
        verbose_name = 'admin profile'
        verbose_name_plural = 'admin profiles'

    def __str__(self):
        return f'{self.user.email} ({self.get_role_display()})'

    @property
    def display_role(self):
        return self.get_role_display()

    @property
    def is_privileged(self):
        return self.role in self.PRIVILEGED_ROLES

    def rotate_session_secret(self):
        """Invalidate every outstanding token issued to this administrator."""
        self.session_secret = generate_session_secret()
        self.save(update_fields=['session_secret', 'updated_at'])


class AdminLoginAttempt(models.Model):
    """
    One row per admin sign-in attempt, successful or not.

    Read by the lockout in ``services_auth.check_login_not_locked``. Rows older
    than the lockout window are pruned by ``tasks.prune_login_attempts`` so the
    table cannot grow without bound under a sustained attack.
    """

    email = models.CharField(max_length=254)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    succeeded = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['email', 'created_at']),
            models.Index(fields=['ip_address', 'created_at']),
        ]

    def __str__(self):
        return f'{self.email} - {"ok" if self.succeeded else "failed"}'


class AdminAuditLog(models.Model):
    """Append-only record of every administrative write."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    admin = models.ForeignKey(
        AdminProfile,
        on_delete=models.SET_NULL,
        related_name='audit_entries',
        null=True,
        blank=True,
    )
    # Kept even when the profile above is deleted, which is the whole point.
    admin_email = models.CharField(max_length=254)
    action = models.CharField(max_length=64)
    target_type = models.CharField(max_length=40, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    summary = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['action', 'created_at']),
            models.Index(fields=['target_type', 'target_id']),
        ]

    def __str__(self):
        return f'{self.admin_email} {self.action} {self.target_type}:{self.target_id}'


class Meeting(models.Model):
    """
    An onboarding call between an administrator and a prospective account.

    ``scheduled_at`` is a single instant rather than a date column plus a time
    column: the reminder task queries on it, and two columns cannot be compared
    against "one hour from now" without reassembling them per row. The console
    is served the date and the time separately by the serializer.

    ``scheduled_at`` is genuinely NULL for a ``REQUESTED`` meeting. A slot that
    has not been agreed is absent, not a placeholder date — a calendar view has
    to be able to put those in an unscheduled column rather than dropping them
    onto today.
    """

    REQUESTED = 'REQUESTED'
    SCHEDULED = 'SCHEDULED'
    COMPLETED = 'COMPLETED'
    CANCELLED = 'CANCELLED'
    NO_SHOW = 'NO_SHOW'

    STATUS_CHOICES = [
        (REQUESTED, 'Requested'),
        (SCHEDULED, 'Scheduled'),
        (COMPLETED, 'Completed'),
        (CANCELLED, 'Cancelled'),
        (NO_SHOW, 'No Show'),
    ]

    # What the meeting decided. Only meaningful once `status` is COMPLETED;
    # it is the answer to "does this account get an activated login?".
    OUTCOME_PENDING = 'PENDING'
    OUTCOME_CONTINUING = 'CONTINUING'
    OUTCOME_NOT_CONTINUING = 'NOT_CONTINUING'
    OUTCOME_FOLLOW_UP = 'FOLLOW_UP'

    OUTCOME_CHOICES = [
        (OUTCOME_PENDING, 'Not recorded'),
        (OUTCOME_CONTINUING, 'Successful - user wants to continue'),
        (OUTCOME_NOT_CONTINUING, 'User does not want to continue'),
        (OUTCOME_FOLLOW_UP, 'Follow-up required'),
    ]

    # Statuses an administrator may set directly. Rescheduling is its own
    # operation and is the only thing that may produce SCHEDULED.
    ADMIN_SETTABLE_STATUSES = frozenset({COMPLETED, CANCELLED, NO_SHOW})

    # Statuses that still expect the call to happen.
    OPEN_STATUSES = frozenset({REQUESTED, SCHEDULED})

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='onboarding_meetings',
    )
    signup_request = models.ForeignKey(
        'accounts.SignupRequest',
        on_delete=models.SET_NULL,
        related_name='meetings',
        null=True,
        blank=True,
    )
    scheduled_at = models.DateTimeField(null=True, blank=True)
    duration_minutes = models.PositiveSmallIntegerField(default=30)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=REQUESTED)
    outcome = models.CharField(
        max_length=20,
        choices=OUTCOME_CHOICES,
        default=OUTCOME_PENDING,
    )
    # What the requester chose on the public form, verbatim, even when it could
    # not be parsed into a slot. Losing it would leave an admin placing a
    # meeting with no idea what the customer actually asked for.
    requested_slot_label = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    user_reminder_sent_at = models.DateTimeField(null=True, blank=True)
    admin_reminder_sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    updated_by = models.ForeignKey(
        AdminProfile,
        on_delete=models.SET_NULL,
        related_name='updated_meetings',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'scheduled_at']),
            models.Index(fields=['user', '-created_at']),
        ]

    def __str__(self):
        return f'{self.user.email} - {self.get_status_display()}'

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES


class AvailabilityRule(models.Model):
    """
    One bookable window on one weekday, in UTC.

    Weekdays follow Python's ``date.weekday()``: Monday is 0. Storing the rule
    rather than the individual slots is what lets the slot length change
    without rewriting a calendar.
    """

    WEEKDAYS = [
        (0, 'Monday'),
        (1, 'Tuesday'),
        (2, 'Wednesday'),
        (3, 'Thursday'),
        (4, 'Friday'),
        (5, 'Saturday'),
        (6, 'Sunday'),
    ]

    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS)
    start_time = models.TimeField()
    end_time = models.TimeField()
    slot_minutes = models.PositiveSmallIntegerField(default=30)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['weekday', 'start_time']
        constraints = [
            models.UniqueConstraint(
                fields=['weekday', 'start_time', 'end_time'],
                name='unique_availability_window_per_weekday',
            ),
            models.CheckConstraint(
                condition=models.Q(end_time__gt=models.F('start_time')),
                name='availability_end_after_start',
            ),
            models.CheckConstraint(
                condition=models.Q(slot_minutes__gte=5, slot_minutes__lte=480),
                name='availability_slot_minutes_range',
            ),
        ]

    def __str__(self):
        return f'{self.get_weekday_display()} {self.start_time}-{self.end_time}'


class AvailabilityBlackout(models.Model):
    """A single date on which no slot is offered, whatever the rules say."""

    date = models.DateField(unique=True)
    reason = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date']

    def __str__(self):
        return f'{self.date} ({self.reason or "unavailable"})'


def prune_expired_login_attempts(older_than):
    """Delete login-attempt rows older than ``older_than`` (a timedelta)."""
    cutoff = timezone.now() - older_than
    deleted, _ = AdminLoginAttempt.objects.filter(created_at__lt=cutoff).delete()
    return deleted
