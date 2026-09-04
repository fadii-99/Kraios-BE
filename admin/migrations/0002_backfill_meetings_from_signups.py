"""
Give every existing signup request the meeting it asked for.

Meetings did not exist before this app, so accounts that signed up earlier hold
a ``SignupRequest`` and nothing else. Without this backfill the console would
report every one of them as "Not Booked", which is not what happened.

The time parser is INLINED rather than imported from ``services_meetings``. A
migration has to keep producing the same result forever, and importing live
application code makes it depend on whatever that code becomes.

Mapping, and the reasoning:

* an approved request became a completed call that went well;
* a rejected request became a completed call that did not;
* anything else keeps its slot if the slot is still ahead, and drops to
  ``REQUESTED`` if it is not - a past slot for a call nobody recorded is a
  meeting that needs re-placing, not one that silently happened.
"""
import re
from datetime import datetime, time
from datetime import timezone as datetime_timezone

from django.db import migrations
from django.utils import timezone


_TIME_PATTERN = re.compile(
    r'^\s*(?P<hour>\d{1,2})\s*[:.]\s*(?P<minute>\d{2})\s*(?P<meridiem>am|pm)?\s*$',
    re.IGNORECASE,
)


_RANGE_SEPARATORS = (' - ', '-', ' to ', '\u2013', '\u2014')


def _parse_slot(label):
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


def backfill(apps, schema_editor):
    SignupRequest = apps.get_model('accounts', 'SignupRequest')
    Meeting = apps.get_model('kraios_admin', 'Meeting')

    now = timezone.now()
    already_covered = set(
        Meeting.objects.exclude(signup_request=None).values_list('signup_request_id', flat=True)
    )

    new_meetings = []
    original_timestamps = {}

    for signup in SignupRequest.objects.exclude(id__in=already_covered).iterator():
        slot_time = _parse_slot(signup.preferred_time)
        scheduled_at = (
            datetime.combine(signup.preferred_date, slot_time, tzinfo=datetime_timezone.utc)
            if slot_time
            else None
        )

        if signup.status == 'APPROVED':
            status, outcome = 'COMPLETED', 'CONTINUING'
        elif signup.status == 'REJECTED':
            status, outcome = 'COMPLETED', 'NOT_CONTINUING'
        elif scheduled_at is not None and scheduled_at > now:
            status, outcome = 'SCHEDULED', 'PENDING'
        else:
            status, outcome = 'REQUESTED', 'PENDING'

        original_timestamps[signup.id] = signup.created_at
        new_meetings.append(
            Meeting(
                user_id=signup.user_id,
                signup_request_id=signup.id,
                scheduled_at=scheduled_at,
                duration_minutes=30,
                status=status,
                outcome=outcome,
                requested_slot_label=str(signup.preferred_time or '')[:100],
                completed_at=now if status == 'COMPLETED' else None,
            )
        )

    if not new_meetings:
        return

    Meeting.objects.bulk_create(new_meetings, batch_size=500)

    # `created_at` is auto_now_add, so bulk_create stamped every backfilled row
    # with "now" and the console would report a two-year-old signup as booked
    # today. bulk_update does not run pre_save, so it is the way to put the
    # real timestamps back.
    restored = list(
        Meeting.objects.filter(signup_request_id__in=original_timestamps.keys())
    )
    for meeting in restored:
        meeting.created_at = original_timestamps[meeting.signup_request_id]
    Meeting.objects.bulk_update(restored, ['created_at'], batch_size=500)


def unbackfill(apps, schema_editor):
    """
    Reversing this migration deletes nothing, deliberately.

    Once the console is live there is no way to tell a meeting this migration
    created from one an administrator booked afterwards - both are linked to a
    signup request. Deleting the set would take real bookings with it, so a
    rollback leaves the rows in place; ``0001_initial`` going backwards drops
    the table anyway.
    """
    return


class Migration(migrations.Migration):

    dependencies = [
        ('kraios_admin', '0001_initial'),
        ('accounts', '0002_alter_user_password'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
