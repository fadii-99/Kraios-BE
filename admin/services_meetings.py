"""
The onboarding meeting: booking it, moving it, closing it, and finding a slot.

WHERE MEETINGS COME FROM. A visitor filling in the public signup form picks a
date and one of the offered times. ``create_meeting_from_signup`` is called
from ``accounts.serializers.SignupRequestSerializer.create`` inside that same
transaction, so a signup request never exists without the meeting it asked for.
It is an explicit call rather than a ``post_save`` signal precisely so it is
greppable from the place that causes it.

ONE SCHEDULE, TWO READERS. What the public form offers and what the console
offers are the same function. ``open_days`` and ``available_slots`` are read by
the console's ``meetings/slots/`` AND by the unauthenticated
``auth/booking/days|slots/`` the signup calendar draws itself from, and
``slot_is_bookable`` is what the signup serializer admits a booking against.
Narrow a rule or close a date in the console and the public form narrows with
it, because there is no second copy of the week to fall out of step.

TIME HANDLING. The public form sends a display label ("09:00 AM") - the label
this module handed it, via ``format_slot_label`` - and the console sends
"14:00". Both are accepted, both are stored as one UTC instant on
``Meeting.scheduled_at``, and the console is always served a 24-hour ``HH:MM``
string back. The original label is kept verbatim on the record so the visitor's
own words survive onto the meeting and into the confirmation email.

The whole product runs in UTC (``settings.TIME_ZONE``). A per-account timezone
is a real requirement later; it is not invented here, and the reminder emails
say "UTC" explicitly rather than implying a local time they cannot know.
"""
import logging
import re
from collections import defaultdict
from datetime import datetime, time, timedelta
from datetime import timezone as datetime_timezone

from django.db import transaction
from django.utils import timezone

from accounts.models import SignupRequest

from .audit import record_admin_action
from .models import AvailabilityBlackout, AvailabilityRule, Meeting


logger = logging.getLogger(__name__)

# How long an onboarding call is assumed to run when nobody says otherwise.
DEFAULT_MEETING_MINUTES = 30

# How far ahead the slot endpoint will look. A booking horizon exists so a
# caller cannot ask for every slot between now and the heat death of the sun.
MAX_SLOT_HORIZON_DAYS = 120

_TIME_PATTERN = re.compile(
    r'^\s*(?P<hour>\d{1,2})\s*[:.]\s*(?P<minute>\d{2})\s*(?P<meridiem>am|pm)?\s*$',
    re.IGNORECASE,
)


# Separators a slot label may use to express a range ("10:00 AM - 11:00 AM").
# The START of the range is the slot; the end is how long it runs, which the
# meeting carries as `duration_minutes` instead.
_RANGE_SEPARATORS = (' - ', '-', ' to ', '\u2013', '\u2014')


def parse_slot_label(label):
    """
    Turn a slot label into a ``time``, or ``None`` when it is not one.

    Accepts "09:00", "9:00", "09:00 AM", "2:30 pm" and a range such as
    "10:00 AM - 11:00 AM", whose start is taken as the slot. Returning None
    rather than guessing at anything else is deliberate: a meeting placed at a
    time nobody agreed to is worse than one an administrator has to place by
    hand, and the original label is kept on the record either way.
    """
    if not label:
        return None

    text = str(label).strip()
    match = _TIME_PATTERN.match(text)

    if not match:
        for separator in _RANGE_SEPARATORS:
            if separator in text:
                match = _TIME_PATTERN.match(text.split(separator, 1)[0])
                if match:
                    break

    if not match:
        return None

    hour = int(match.group('hour'))
    minute = int(match.group('minute'))
    meridiem = (match.group('meridiem') or '').lower()

    if meridiem:
        if hour < 1 or hour > 12:
            return None
        if meridiem == 'pm' and hour != 12:
            hour += 12
        elif meridiem == 'am' and hour == 12:
            hour = 0
    elif hour > 23:
        return None

    if minute > 59:
        return None

    return time(hour=hour, minute=minute)


def format_slot_label(slot_time):
    """
    The 12-hour label a slot is SHOWN and BOOKED under - "14:00" -> "02:00 PM".

    The inverse of ``parse_slot_label``, and the reason the public form can go
    on submitting a display string: the visitor picks what they read, the
    record keeps what they picked, and the confirmation email quotes it back
    without reformatting a time nobody typed.
    """
    if slot_time is None:
        return ''

    hour = slot_time.hour % 12 or 12
    meridiem = 'AM' if slot_time.hour < 12 else 'PM'

    return f'{hour:02d}:{slot_time.minute:02d} {meridiem}'


def combine_to_utc(day, slot_time):
    """A date plus a wall-clock time as one aware UTC instant."""
    if day is None or slot_time is None:
        return None
    # `django.utils.timezone.utc` was removed in Django 5; the stdlib value
    # is the supported way to build an aware UTC datetime.
    return datetime.combine(day, slot_time, tzinfo=datetime_timezone.utc)


def create_meeting_from_signup(signup_request):
    """
    Create the onboarding meeting a signup asked for.

    A parsable slot in the future is booked outright, because that is what the
    public form promises the visitor and what the confirmation email says. A
    slot that cannot be parsed, or one already in the past, produces a
    ``REQUESTED`` meeting with no instant: an administrator then places it, and
    the console lists unplaced requests at the top for exactly that reason.

    Nothing reaching this from the signup form should take that second branch
    any more - the serializer rejects a slot the schedule does not offer before
    it gets here. It is kept as the floor rather than raised to an assertion:
    the fallback costs an administrator one placement, and an exception here
    would cost the visitor their sign-up.
    """
    slot_time = parse_slot_label(signup_request.preferred_time)
    scheduled_at = combine_to_utc(signup_request.preferred_date, slot_time)

    if scheduled_at is not None and scheduled_at <= timezone.now():
        scheduled_at = None

    status = Meeting.SCHEDULED if scheduled_at else Meeting.REQUESTED

    # `SignupRequest.status` is deliberately left alone. It tracks the
    # ADMISSION decision - pending, then approved or rejected once the call has
    # happened - while the meeting tracks the scheduling lifecycle. Two facts,
    # two columns; collapsing them would mean an approved account could not
    # also have a cancelled meeting on record.
    return Meeting.objects.create(
        user=signup_request.user,
        signup_request=signup_request,
        scheduled_at=scheduled_at,
        duration_minutes=DEFAULT_MEETING_MINUTES,
        status=status,
        requested_slot_label=str(signup_request.preferred_time or '')[:100],
    )


def latest_meeting_by_user(user_ids):
    """
    ``{user_id: Meeting}`` holding each account's MOST RECENT meeting.

    An account can hold several - a cancellation and a rebooking - and the
    console reports the newest, so a rebooked account reads as Scheduled rather
    than staying Cancelled forever. One query, ordered so the newest wins the
    dictionary assignment.
    """
    meetings = (
        Meeting.objects.filter(user_id__in=list(user_ids))
        .order_by('user_id', 'created_at')
    )

    latest = {}
    for meeting in meetings:
        latest[meeting.user_id] = meeting
    return latest


@transaction.atomic
def set_meeting_status(meeting, status, admin_profile, *, outcome=None, notes=None, request=None):
    """
    Close a meeting out: completed, cancelled, or a no-show.

    Rescheduling is NOT reachable from here - it is its own operation, because
    giving a meeting a new slot and recording how the last one went are
    different acts with different consequences.
    """
    if status not in Meeting.ADMIN_SETTABLE_STATUSES:
        raise ValueError('unsupported status')

    previous_status = meeting.status
    now = timezone.now()

    meeting.status = status
    meeting.updated_by = admin_profile

    if status == Meeting.COMPLETED:
        meeting.completed_at = now
        if outcome:
            meeting.outcome = outcome
    elif status == Meeting.CANCELLED:
        meeting.cancelled_at = now
    elif status == Meeting.NO_SHOW:
        meeting.completed_at = now

    if notes is not None:
        meeting.notes = notes

    meeting.save()

    # The signup request follows the meeting, so the Users page's "signup
    # status" column and the meeting column cannot contradict each other.
    _sync_signup_status(meeting)

    record_admin_action(
        admin_profile,
        'meeting.status_change',
        target_type='meeting',
        target_id=meeting.pk,
        summary=f'{previous_status} -> {status} for {meeting.user.email}',
        metadata={'from': previous_status, 'to': status, 'outcome': meeting.outcome},
        request=request,
    )
    return meeting


def _sync_signup_status(meeting):
    """
    Carry a recorded OUTCOME through to the signup request.

    Only an outcome moves it: a completed call where the customer wants to
    continue approves the request, one where they do not rejects it. Booking,
    cancelling and rebooking leave it alone, because none of those is a
    decision about whether the account is admitted.
    """
    signup_request = meeting.signup_request
    if signup_request is None or meeting.status != Meeting.COMPLETED:
        return

    outcomes = {
        Meeting.OUTCOME_CONTINUING: SignupRequest.APPROVED,
        Meeting.OUTCOME_NOT_CONTINUING: SignupRequest.REJECTED,
    }
    target = outcomes.get(meeting.outcome)

    if target and target != signup_request.status:
        signup_request.status = target
        signup_request.save(update_fields=['status', 'updated_at'])


@transaction.atomic
def reschedule_meeting(meeting, scheduled_at, admin_profile, *, request=None):
    """
    Give a meeting a slot.

    Rescheduling always lands it in ``SCHEDULED``, including from ``CANCELLED``
    and ``NO_SHOW``: rebooking a call somebody missed is the normal case, and
    leaving the old status on a future date would make the list lie.

    The reminder flags are cleared, because a reminder already sent was for a
    different time and the new one still has to go out.
    """
    previous = meeting.scheduled_at

    meeting.scheduled_at = scheduled_at
    meeting.status = Meeting.SCHEDULED
    meeting.cancelled_at = None
    meeting.completed_at = None
    meeting.user_reminder_sent_at = None
    meeting.admin_reminder_sent_at = None
    meeting.updated_by = admin_profile
    meeting.save()

    _sync_signup_status(meeting)

    record_admin_action(
        admin_profile,
        'meeting.reschedule',
        target_type='meeting',
        target_id=meeting.pk,
        summary=f'Rescheduled the call with {meeting.user.email}',
        metadata={
            'from': previous.isoformat() if previous else None,
            'to': scheduled_at.isoformat(),
        },
        request=request,
    )
    return meeting


def booked_instants(day):
    """Every instant already taken by an open meeting on ``day`` (UTC)."""
    start = combine_to_utc(day, time(0, 0))
    end = start + timedelta(days=1)

    return _booked_instants_between(start, end)


def _booked_instants_between(start, end):
    """Every instant an open meeting holds in ``[start, end)`` (UTC)."""
    return set(
        Meeting.objects.filter(
            status__in=Meeting.OPEN_STATUSES,
            scheduled_at__gte=start,
            scheduled_at__lt=end,
        ).values_list('scheduled_at', flat=True)
    )


def _slot_instants(day, rules):
    """
    The UTC instants ``rules`` put on ``day``, in order and without repeats.

    Windows on the same weekday may overlap - two rules covering 09:00-12:00
    and 11:00-17:00 both produce 11:00 - and a slot offered twice would be
    bookable twice. Deduplicating on the instant is what makes the slot list a
    set of times rather than a concatenation of windows.
    """
    seen = set()

    for rule in rules:
        cursor = combine_to_utc(day, rule.start_time)
        window_end = combine_to_utc(day, rule.end_time)
        step = timedelta(minutes=rule.slot_minutes)

        while cursor + step <= window_end:
            seen.add(cursor)
            cursor += step

    return sorted(seen)


def available_slots(day, *, now=None):
    """
    The free slots on one date, as ``[{'time': 'HH:MM', 'available': bool}]``.

    A slot is offered when a rule covers it, no blackout claims the date, no
    open meeting already holds it, and it has not already passed. All four are
    reported through ``available`` rather than by omission, so the console can
    show a taken slot as taken instead of silently not showing it.
    """
    now = now or timezone.now()

    if AvailabilityBlackout.objects.filter(date=day).exists():
        return []

    rules = list(AvailabilityRule.objects.filter(weekday=day.weekday(), is_active=True))
    if not rules:
        return []

    taken = booked_instants(day)

    return [
        {
            'time': instant.strftime('%H:%M'),
            'available': instant not in taken and instant > now,
        }
        for instant in _slot_instants(day, rules)
    ]


def open_days(start, end, *, now=None):
    """
    The dates in ``[start, end]`` that still have at least one free slot.

    This is what the PUBLIC signup calendar greys itself out from, so it has to
    agree with ``available_slots`` exactly: a date offered here and empty there
    is a visitor clicking a day and being told there is nothing on it.

    THREE QUERIES FOR THE WHOLE RANGE, not three per date. A month view asks
    about thirty-odd dates at once, and calling ``available_slots`` in a loop
    would make one calendar cost ninety round trips. The rules and blackouts
    are read once and the grid is walked in Python.
    """
    now = now or timezone.now()

    if end < start:
        return []

    rules_by_weekday = defaultdict(list)
    for rule in AvailabilityRule.objects.filter(is_active=True):
        rules_by_weekday[rule.weekday].append(rule)

    if not rules_by_weekday:
        return []

    blackouts = set(
        AvailabilityBlackout.objects.filter(
            date__gte=start,
            date__lte=end,
        ).values_list('date', flat=True)
    )

    taken = _booked_instants_between(
        combine_to_utc(start, time(0, 0)),
        combine_to_utc(end, time(0, 0)) + timedelta(days=1),
    )

    days = []
    cursor = start

    while cursor <= end:
        rules = rules_by_weekday.get(cursor.weekday())
        if rules and cursor not in blackouts:
            if any(
                instant not in taken and instant > now
                for instant in _slot_instants(cursor, rules)
            ):
                days.append(cursor)
        cursor += timedelta(days=1)

    return days


def slot_is_bookable(day, slot_time, *, now=None):
    """
    Whether ``slot_time`` on ``day`` is a slot the schedule actually offers.

    The signup form only ever shows bookable slots, so reaching this with a
    False means the request did not come from that form as drawn - a stale tab,
    a slot taken while the visitor was deciding, or a handwritten POST. All
    three get the same answer, because the schedule is the authority and the
    form is only a view of it.
    """
    if day is None or slot_time is None:
        return False

    wanted = slot_time.strftime('%H:%M')

    return any(
        slot['time'] == wanted and slot['available']
        for slot in available_slots(day, now=now)
    )


def due_reminders(lead_minutes, grace_minutes):
    """
    Meetings whose user reminder is due now.

    The window has a tail (``grace_minutes``) so a worker that was down for a
    few minutes still sends, and a head cut at the meeting time itself so a
    reminder is never sent for a call that has already started.
    """
    now = timezone.now()
    return (
        Meeting.objects.filter(
            status=Meeting.SCHEDULED,
            user_reminder_sent_at__isnull=True,
            scheduled_at__isnull=False,
            scheduled_at__gt=now,
            scheduled_at__lte=now + timedelta(minutes=lead_minutes + grace_minutes),
        )
        .select_related('user')
        .order_by('scheduled_at')
    )
