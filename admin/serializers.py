"""
Request payload validation for the admin console.

These serializers exist to reject bad input, not to describe responses -
responses are built by ``representations.py``. Each one is an explicit
ALLOWLIST: a key the console does not send cannot arrive here and reach a model
write, which is what keeps ``is_active``, ``password`` and ``role`` out of an
edit form's reach.

Field names match what the console already sends, so the browser code changes
only where the endpoint URL changes and never because a key was renamed.

Where a rule already exists elsewhere it is imported rather than restated: plan
validation lives in ``dummy_data.validate_plan`` and is called from the view,
because the form is not the only caller and a rule written twice is a rule that
will disagree with itself.
"""
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from . import dummy_data
from .models import Meeting
from .services_users import ACCOUNT_STATUSES


class AdminLoginSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=128)

    def validate_email(self, email):
        return email.strip().lower()


class AdminPasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=128)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=128)

    def validate(self, attrs):
        if attrs['current_password'] == attrs['new_password']:
            raise serializers.ValidationError(
                {'new_password': 'Choose a password you have not used here before.'}
            )
        validate_password(attrs['new_password'], user=self.context.get('admin_user'))
        return attrs


class UserUpdateSerializer(serializers.Serializer):
    """
    The Edit User form.

    Every field is optional: a form that does not carry a field must not blank
    it. ``subscription`` is the plan NAME the console's dropdown holds, and is
    applied only when it differs from the account's current plan - editing a
    name must not silently restart somebody's billing period.
    """

    name = serializers.CharField(max_length=150, required=False, allow_blank=False)
    firm = serializers.CharField(max_length=150, required=False, allow_blank=False)
    email = serializers.EmailField(max_length=254, required=False)
    country = serializers.CharField(max_length=100, required=False, allow_blank=False)
    jobTitle = serializers.CharField(max_length=100, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True)
    accountStatus = serializers.ChoiceField(choices=ACCOUNT_STATUSES, required=False)
    subscription = serializers.CharField(max_length=80, required=False, allow_blank=True)

    def to_model_changes(self):
        """The validated payload, keyed by model field name."""
        data = self.validated_data
        mapping = {
            'name': 'full_name',
            'firm': 'firm_name',
            'email': 'email',
            'country': 'country',
            'jobTitle': 'job_title',
            'phone': 'phone',
        }
        return {
            model_field: data[payload_key]
            for payload_key, model_field in mapping.items()
            if payload_key in data
        }


class UserStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=ACCOUNT_STATUSES)


class SubscriptionAssignSerializer(serializers.Serializer):
    """
    Assign a plan for a fixed period.

    A plan is named either by id (what the console's plan list carries) or by
    display name (what the Change Subscription dropdown carries). Exactly one
    is required, and the view resolves it against the catalogue.
    """

    planId = serializers.CharField(max_length=64, required=False, allow_blank=True)
    plan = serializers.CharField(max_length=80, required=False, allow_blank=True)
    billingCycle = serializers.ChoiceField(
        choices=dummy_data.BILLING_CYCLES,
        default='Monthly',
    )
    durationDays = serializers.IntegerField(
        required=False,
        min_value=dummy_data.MIN_SUBSCRIPTION_DAYS,
        max_value=dummy_data.MAX_SUBSCRIPTION_DAYS,
        default=dummy_data.DEFAULT_SUBSCRIPTION_DAYS,
    )

    def validate(self, attrs):
        if not attrs.get('planId') and not attrs.get('plan'):
            raise serializers.ValidationError(
                {'planId': 'Name the plan to assign, by id or by name.'}
            )
        return attrs


class MeetingStatusSerializer(serializers.Serializer):
    """
    Close a meeting out.

    ``Scheduled`` is deliberately not offered: giving a meeting a slot is the
    reschedule operation, and allowing it here would let a caller mark a
    meeting scheduled without ever saying when.
    """

    status = serializers.ChoiceField(
        choices=[
            (Meeting.COMPLETED, 'Completed'),
            (Meeting.CANCELLED, 'Cancelled'),
            (Meeting.NO_SHOW, 'No Show'),
        ],
    )
    outcome = serializers.ChoiceField(
        choices=[choice[0] for choice in Meeting.OUTCOME_CHOICES],
        required=False,
    )
    notes = serializers.CharField(max_length=4000, required=False, allow_blank=True)

    def to_internal_value(self, data):
        # The console sends the display label ("No Show"); the API also accepts
        # the stored key ("NO_SHOW"), so a machine caller need not know the
        # console's wording.
        # `.copy()` rather than `dict()`: a form-encoded body arrives as a
        # QueryDict, and `dict(QueryDict)` turns every value into a list.
        payload = data.copy() if hasattr(data, 'copy') else dict(data)
        labels = {label: key for key, label in Meeting.STATUS_CHOICES}
        if payload.get('status') in labels:
            payload['status'] = labels[payload['status']]
        return super().to_internal_value(payload)


class MeetingRescheduleSerializer(serializers.Serializer):
    date = serializers.DateField()
    time = serializers.CharField(max_length=20)
    notes = serializers.CharField(max_length=4000, required=False, allow_blank=True)


class MeetingNotesSerializer(serializers.Serializer):
    notes = serializers.CharField(max_length=4000, allow_blank=True)


class PlanStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=dummy_data.PLAN_STATUSES)


class SupportTriageSerializer(serializers.Serializer):
    """
    One triage change at a time.

    Status, priority and assignment are three independent facts and the console
    saves each on its own, so requiring all three would make changing a
    priority feel like filing a document.
    """

    status = serializers.ChoiceField(choices=dummy_data.SUPPORT_STATUSES, required=False)
    priority = serializers.ChoiceField(choices=dummy_data.SUPPORT_PRIORITIES, required=False)
    assignee = serializers.CharField(max_length=120, required=False, allow_blank=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError('Name at least one field to change.')
        return attrs


class ContactRequestSerializer(serializers.Serializer):
    """
    The PUBLIC contact form — the one payload here that a stranger sends.

    Every other serializer in this module is filled in by a signed-in
    administrator, so this one is the allowlist that matters most: `status`,
    `priority`, `assignee`, `submittedAt` and `id` are absent by design and are
    set by ``dummy_data.create_support_request``. A sender who could name their
    own priority would name Urgent.

    Lengths are capped at the field rather than truncated afterwards, so an
    oversized field is a 400 the form can point at and not a record silently
    missing its end. `message` has a FLOOR as well: "hi" is not an enquiry
    anybody can action, and asking for a sentence up front is cheaper than an
    administrator asking for one by email.
    """

    name = serializers.CharField(max_length=150)
    email = serializers.EmailField(max_length=254)
    firm = serializers.CharField(max_length=150)
    country = serializers.CharField(max_length=100)
    topic = serializers.ChoiceField(choices=dummy_data.SUPPORT_TOPICS)
    subject = serializers.CharField(max_length=150)
    message = serializers.CharField(max_length=4000, min_length=10)

    def validate_email(self, email):
        return email.strip().lower()


class AvailabilityRuleSerializer(serializers.Serializer):
    weekday = serializers.IntegerField(min_value=0, max_value=6)
    startTime = serializers.TimeField()
    endTime = serializers.TimeField()
    slotMinutes = serializers.IntegerField(min_value=5, max_value=480, default=30)
    isActive = serializers.BooleanField(default=True)

    def validate(self, attrs):
        if attrs['endTime'] <= attrs['startTime']:
            raise serializers.ValidationError(
                {'endTime': 'The window has to end after it starts.'}
            )
        return attrs

    def to_model_fields(self):
        data = self.validated_data
        return {
            'weekday': data['weekday'],
            'start_time': data['startTime'],
            'end_time': data['endTime'],
            'slot_minutes': data['slotMinutes'],
            'is_active': data['isActive'],
        }


class AvailabilityBlackoutSerializer(serializers.Serializer):
    date = serializers.DateField()
    reason = serializers.CharField(max_length=150, required=False, allow_blank=True)


class AvailabilityWeekSerializer(serializers.Serializer):
    """A full replacement of the weekly schedule, sent as one document."""

    rules = AvailabilityRuleSerializer(many=True)

    def validate_rules(self, rules):
        if len(rules) > 7 * 6:
            raise serializers.ValidationError('That is more windows than a week can hold.')

        # The model has a uniqueness constraint on (weekday, start, end).
        # Catching a repeat here turns it into a field error rather than an
        # IntegrityError halfway through replacing the week.
        seen = set()
        for rule in rules:
            key = (rule['weekday'], rule['startTime'], rule['endTime'])
            if key in seen:
                raise serializers.ValidationError(
                    'The same window is listed twice for one day.'
                )
            seen.add(key)

        return rules
