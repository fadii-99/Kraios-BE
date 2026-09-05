"""
Everything the console does to a customer account.

ACCOUNT LIFECYCLE. The product's real flow is: a visitor books a walkthrough,
the call happens, and only if it goes well does an administrator issue
credentials. Until then the account exists but cannot sign in - it has no
usable password and ``is_active`` is False. ``derive_setup_state`` turns that
sequence into the single word the console prints, and it is the only place that
mapping exists so the Users table and the account page cannot disagree.

PASSWORD ISSUE. ``generate_user_password`` is the one operation that creates a
credential. The plaintext exists inside that function and inside the email it
sends, and nowhere else: it is not returned to the caller, not written to the
audit trail, and not logged. If the email cannot be delivered the account is
still activated and the caller is told the message failed, so an administrator
retries rather than assuming the customer has it.

ADMINISTRATORS ARE NOT CUSTOMERS. Every queryset here excludes users holding an
``AdminProfile``. That is not cosmetic: it is what stops the console offering
"deactivate" on the account of the person using it, and it means an
administrator can never be edited through an endpoint intended for customers.
"""
import logging
import secrets

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction

from accounts.models import SignupRequest, User
from profiles.services import blacklist_user_refresh_tokens

from . import dummy_data
from .audit import record_admin_action
from .emails import send_subscription_activated, send_user_credentials
from .models import Meeting


logger = logging.getLogger(__name__)

# The console's account-status vocabulary, which maps onto `User.is_active`.
ACCOUNT_ACTIVE = 'Active'
ACCOUNT_INACTIVE = 'Inactive'
ACCOUNT_STATUSES = (ACCOUNT_ACTIVE, ACCOUNT_INACTIVE)

# The setup states the console prints. See `derive_setup_state` for the ladder.
SETUP_SIGNUP_PENDING = 'Signup Pending'
SETUP_VERIFICATION_PENDING = 'Verification Pending'
SETUP_PASSWORD_PENDING = 'Password Setup Pending'
SETUP_READY = 'Ready'
SETUP_ACTIVE = 'Active'
SETUP_INACTIVE = 'Inactive'

MEETING_NOT_BOOKED = 'Not Booked'

# Fields an administrator may write on a customer record. Anything absent from
# this tuple - `is_active`, `password`, `role`, `date_joined` - is changed only
# through the operation that owns it, never by a field in an edit form.
EDITABLE_USER_FIELDS = ('full_name', 'firm_name', 'country', 'job_title', 'phone')

# Generated passwords: long enough that the length alone defeats guessing, and
# drawn from a set that satisfies Django's validators without needing a retry
# loop. Ambiguous glyphs are excluded because this password is read off a
# screen and typed by hand.
PASSWORD_LENGTH = 16
_LOWER = 'abcdefghijkmnopqrstuvwxyz'
_UPPER = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
_DIGITS = '23456789'
# No `&`, `<`, `>` or quotes: a generated password is rendered into an email
# through Django's template engine, which HTML-escapes by default, and a
# customer typing `&amp;` because that is what the message showed them is a
# support ticket nobody can diagnose. The plain-text template disables
# autoescaping as well - both, because either one alone is a single point of
# failure for a value that cannot be recovered once it has been sent.
_SYMBOLS = '!@#$%^*-_=+?'
_PASSWORD_ALPHABET = _LOWER + _UPPER + _DIGITS + _SYMBOLS


class UserOperationError(Exception):
    """A refusal with caller-safe text."""

    def __init__(self, message, http_status=400, field_errors=None):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.field_errors = field_errors or {}


def customer_queryset():
    """Every account the console manages - administrators excluded."""
    return User.objects.filter(admin_profile__isnull=True)


def generate_secure_password(length=PASSWORD_LENGTH):
    """
    A cryptographically random password containing all four character classes.

    ``secrets`` rather than ``random``: this value is a live credential, and
    copying a predictable generator into a place that issues credentials is a
    mistake somebody makes exactly once.
    """
    required = [
        secrets.choice(_LOWER),
        secrets.choice(_UPPER),
        secrets.choice(_DIGITS),
        secrets.choice(_SYMBOLS),
    ]
    remaining = [
        secrets.choice(_PASSWORD_ALPHABET)
        for _ in range(max(0, length - len(required)))
    ]
    characters = required + remaining

    # Shuffle with `secrets` too, so the first four positions do not always
    # hold one of each class.
    for index in range(len(characters) - 1, 0, -1):
        swap = secrets.randbelow(index + 1)
        characters[index], characters[swap] = characters[swap], characters[index]

    return ''.join(characters)


def derive_setup_state(user, latest_meeting):
    """
    The one word the console prints for where an account is in onboarding.

    CREDENTIALS ARE THE HINGE, not ``is_active``. A pending signup and a
    deactivated customer are both ``is_active=False``, and reading only that
    flag would collapse the entire onboarding sequence into "Inactive" - which
    is what the Users page showed before this function looked at the password.
    An account that has never been issued credentials is still in onboarding;
    one that has them and is switched off was deactivated.

    Credentials issued:

    ``Inactive``                 switched off by an administrator.
    ``Active``                   can sign in and has done so at least once.
    ``Password Setup Pending``   credentials sent, first sign-in awaited.

    No credentials yet - the state is whatever the meeting says:

    ``Ready``                    the call went well; waiting on an
                                 administrator to issue credentials.
    ``Inactive``                 the call happened and the customer declined.
    ``Verification Pending``     the call is booked; the call IS the
                                 verification step in this product.
    ``Signup Pending``           signed up, no slot agreed, or the last call
                                 fell through and needs rebooking.
    """
    if user.has_usable_password():
        if not user.is_active:
            return SETUP_INACTIVE
        return SETUP_ACTIVE if user.last_login else SETUP_PASSWORD_PENDING

    if latest_meeting is None:
        return SETUP_SIGNUP_PENDING

    if latest_meeting.status == Meeting.COMPLETED:
        if latest_meeting.outcome == Meeting.OUTCOME_CONTINUING:
            return SETUP_READY
        if latest_meeting.outcome == Meeting.OUTCOME_NOT_CONTINUING:
            return SETUP_INACTIVE
        return SETUP_SIGNUP_PENDING

    if latest_meeting.status == Meeting.SCHEDULED:
        return SETUP_VERIFICATION_PENDING

    # Requested, cancelled or a no-show: all three are waiting on a slot being
    # agreed, which is what Signup Pending means here.
    return SETUP_SIGNUP_PENDING


def derive_signup_status(signup_request):
    """``SignupRequest.status`` in the console's three-word vocabulary."""
    if signup_request is None:
        return 'Requested'
    if signup_request.status == SignupRequest.APPROVED:
        return 'Approved'
    if signup_request.status == SignupRequest.REJECTED:
        return 'Rejected'
    return 'Requested'


def derive_meeting_status(latest_meeting):
    if latest_meeting is None:
        return MEETING_NOT_BOOKED
    return latest_meeting.get_status_display()


@transaction.atomic
def update_user(user, changes, admin_profile, *, request=None):
    """
    Apply an edit to a customer record.

    Only the fields in ``EDITABLE_USER_FIELDS`` plus ``email`` are touched, and
    each one has to be present in ``changes`` - a form that does not carry a
    field cannot blank it.
    """
    updated_fields = []

    for field in EDITABLE_USER_FIELDS:
        if field in changes and changes[field] is not None:
            value = str(changes[field]).strip()
            if getattr(user, field) != value:
                setattr(user, field, value)
                updated_fields.append(field)

    if 'email' in changes and changes['email']:
        email = str(changes['email']).strip().lower()
        if email != user.email:
            if User.objects.filter(email__iexact=email).exclude(pk=user.pk).exists():
                raise UserOperationError(
                    'That email address is already registered to another account.',
                    http_status=409,
                    field_errors={'email': 'Already registered to another account'},
                )
            user.email = email
            updated_fields.append('email')

    if updated_fields:
        user.save(update_fields=updated_fields)
        record_admin_action(
            admin_profile,
            'user.update',
            target_type='user',
            target_id=user.pk,
            summary=f'Updated {", ".join(updated_fields)} on {user.email}',
            metadata={'fields': updated_fields},
            request=request,
        )

    return user


@transaction.atomic
def set_user_status(user, status, admin_profile, *, request=None):
    """
    Switch an account on or off.

    Deactivating blacklists the account's refresh tokens, so an open session
    stops working at its next refresh rather than surviving for the life of a
    token that was issued before the decision. It is a separate operation from
    ``update_user`` because it is a different act: one saves what an
    administrator typed, this decides whether somebody can sign in.
    """
    if status not in ACCOUNT_STATUSES:
        raise UserOperationError('Unsupported account status.')

    should_be_active = status == ACCOUNT_ACTIVE
    if user.is_active == should_be_active:
        return user

    user.is_active = should_be_active
    user.save(update_fields=['is_active'])

    if not should_be_active:
        blacklist_user_refresh_tokens(user)

    record_admin_action(
        admin_profile,
        'user.activate' if should_be_active else 'user.deactivate',
        target_type='user',
        target_id=user.pk,
        summary=f'{status} - {user.email}',
        request=request,
    )
    return user


def generate_user_password(user, admin_profile, *, request=None):
    """
    Issue credentials for an account and activate it.

    Returns ``(user, credentials_email_sent)``. The password itself is
    deliberately not returned: the email is the delivery channel, and a
    password that also comes back through the API would sit in a browser's
    memory, in an access log's response size, and in whatever the console
    happened to store.

    Calling it again issues a NEW password and invalidates the old one, which
    is the recovery path when a message does not arrive.
    """
    raw_password = generate_secure_password()

    try:
        validate_password(raw_password, user=user)
    except DjangoValidationError:
        # The generator satisfies every shipped validator, so this can only
        # fire if a project-specific validator is added later. Fail loudly
        # rather than emailing a password the login page will refuse.
        logger.exception('Generated password rejected by the configured validators.')
        raise UserOperationError(
            'Could not generate a password that meets the configured policy.',
            http_status=500,
        )

    with transaction.atomic():
        user.set_password(raw_password)
        user.is_active = True
        user.save(update_fields=['password', 'is_active'])

        # Any session opened with a previous credential dies with it.
        blacklist_user_refresh_tokens(user)

        signup_request = SignupRequest.objects.filter(user=user).first()
        if signup_request is not None and signup_request.status != SignupRequest.APPROVED:
            signup_request.status = SignupRequest.APPROVED
            signup_request.save(update_fields=['status', 'updated_at'])

        record_admin_action(
            admin_profile,
            'user.generate_password',
            target_type='user',
            target_id=user.pk,
            summary=f'Issued credentials to {user.email}',
            request=request,
        )

    # Sent only after the write is committed, so a customer can never receive a
    # password that a rolled-back transaction never set.
    try:
        send_user_credentials(user, raw_password, settings.KRAIOS_APP_SIGN_IN_URL)
        email_sent = True
    except Exception:
        email_sent = False
        logger.exception('Could not deliver credentials to user %s.', user.pk)

    # Nothing below this line may reference `raw_password`.
    del raw_password
    return user, email_sent


def assign_subscription(user, plan_id, billing_cycle, admin_profile, *, request=None):
    """
    Put an account on a plan for the period its billing cycle names.

    The cycle is the ONLY period input - see ``dummy_data.assign_subscription``.
    The record lives in the placeholder store, not the database; the audit
    entry, which IS in the database, is what makes the assignment traceable in
    the meantime, and it records the resolved end date rather than the cycle
    alone so a later reader does not have to know today's mapping.
    """
    subscription, errors = dummy_data.assign_subscription(
        user.pk,
        plan_id,
        billing_cycle,
        assigned_by=admin_profile.user.email,
    )

    if errors:
        raise UserOperationError(
            'That subscription could not be assigned.',
            field_errors=errors,
        )

    record_admin_action(
        admin_profile,
        'subscription.assign',
        target_type='user',
        target_id=user.pk,
        summary=f'{subscription["plan"]} ({subscription["billingCycle"]}) until {subscription["renewalDate"]} - {user.email}',
        metadata={
            'planId': subscription['planId'],
            'billingCycle': subscription['billingCycle'],
            'durationDays': subscription['durationDays'],
            'renewalDate': subscription['renewalDate'],
        },
        request=request,
    )

    try:
        send_subscription_activated(user, subscription)
    except Exception:
        # A missing confirmation email does not un-assign the plan.
        logger.exception('Could not send the subscription email to user %s.', user.pk)

    return subscription


def clear_subscription(user, admin_profile, *, request=None):
    removed = dummy_data.clear_subscription(user.pk)
    if removed:
        record_admin_action(
            admin_profile,
            'subscription.clear',
            target_type='user',
            target_id=user.pk,
            summary=f'Removed the subscription on {user.email}',
            request=request,
        )
    return removed


def resolve_plan_by_name(plan_name):
    """The plan record matching a display name, or ``None``."""
    if not plan_name or plan_name == 'None':
        return None
    wanted = str(plan_name).strip().lower()
    for plan in dummy_data.list_plans():
        if plan['name'].lower() == wanted:
            return plan
    return None
