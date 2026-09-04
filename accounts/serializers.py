from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers

# The administrator's schedule decides what the public form may book. Imported
# at module scope because these two read availability and touch nothing in the
# admin app that imports back into `accounts.serializers`; the meeting-creating
# call in `create` still has to be deferred - see the comment there.
from admin.services_meetings import parse_slot_label, slot_is_bookable

from .models import SignupRequest, User


class SignupRequestSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    firm = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    country = serializers.CharField(max_length=100)
    date = serializers.DateField()
    time = serializers.CharField(max_length=100)

    def validate_email(self, email):
        normalized_email = email.lower()

        if User.objects.filter(email__iexact=normalized_email).exists():
            raise serializers.ValidationError(
                'An account with this email address already exists.'
            )

        return normalized_email

    def validate(self, attrs):
        """
        The chosen slot must be one the administrator actually offers.

        Checked ACROSS the two fields rather than in `validate_time`, because
        neither is bookable alone: "10:00 AM" is a real slot on a Tuesday and
        nothing at all on a Sunday, and the answer is the pair.

        The form only ever draws bookable slots, so a failure here is a stale
        tab, a slot taken while the visitor was deciding, or a request that
        never came from the form. The message names the fix - pick again - and
        does not explain which of the three it was.
        """
        slot_time = parse_slot_label(attrs.get('time'))
        if slot_time is None:
            raise serializers.ValidationError(
                {'time': 'Choose one of the times offered for that date.'}
            )

        if not slot_is_bookable(attrs.get('date'), slot_time):
            raise serializers.ValidationError({
                'time': (
                    'That time is no longer available. '
                    'Pick another slot from the times shown.'
                )
            })

        return attrs

    def create(self, validated_data):
        with transaction.atomic():
            # No password and not active. Signing up books a walkthrough; it
            # does not create a login. Credentials are issued by an
            # administrator after the call, through the admin console
            # (`admin.services_users.generate_user_password`), and only then
            # does the account become usable.
            #
            # `password=None` stores an UNUSABLE password hash, so there is no
            # value for `check_password` to ever match - not a shared default
            # that would let anybody sign in as any pending signup.
            user = User.objects.create_user(
                email=validated_data['email'],
                password=None,
                full_name=validated_data['name'],
                firm_name=validated_data['firm'],
                country=validated_data['country'],
                is_active=False,
            )

            signup_request = SignupRequest.objects.create(
                user=user,
                preferred_date=validated_data['date'],
                preferred_time=validated_data['time'],
            )

            # Imported here rather than at module scope: the admin app imports
            # `accounts.models`, and a top-level import in both directions
            # would be a cycle. Called explicitly rather than through a signal
            # so the meeting's origin is greppable from the place that causes it.
            from admin.services_meetings import create_meeting_from_signup

            # Asked AGAIN, inside the transaction. `validate` ran before this
            # one opened, and two visitors submitting the same slot at the same
            # moment would both have passed it. Re-checking here narrows that
            # to the width of one transaction and rolls the user row back with
            # the meeting, so a lost race leaves nothing behind for the second
            # visitor's retry to collide with on their email address.
            if not slot_is_bookable(
                validated_data['date'],
                parse_slot_label(validated_data['time']),
            ):
                raise serializers.ValidationError({
                    'time': (
                        'That time was taken while you were booking. '
                        'Pick another slot from the times shown.'
                    )
                })

            create_meeting_from_signup(signup_request)

        return signup_request


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_email(self, email):
        return email.lower()


class ForgotPasswordRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, email):
        return email.lower()


class ForgotPasswordConfirmSerializer(serializers.Serializer):
    email = serializers.EmailField()
    verification_id = serializers.UUIDField()
    otp = serializers.RegexField(
        regex=r'^\d{6}$',
        write_only=True,
        error_messages={'invalid': 'OTP must contain exactly 6 digits.'},
    )
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_email(self, email):
        return email.lower()

    def validate(self, attrs):
        user = self.context.get('user')
        if user is not None:
            validate_password(attrs['new_password'], user=user)
        return attrs


class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            'id',
            'email',
            'full_name',
            'firm_name',
            'country',
            'job_title',
            'phone',
            'role',
            'is_active',
        )
        read_only_fields = fields
