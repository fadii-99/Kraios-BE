"""
Seed a working week so the slot endpoint has something to answer with.

Without any rule, ``available_slots`` correctly returns nothing - and an
administrator opening the reschedule dialog on a fresh install would see an
empty list with no explanation. Monday to Friday, 09:00-17:00 UTC in half-hour
slots is a defensible starting point that the console can then replace outright
through ``PUT /api/v1/admin/availability/``.

Only seeded when the table is empty, so re-running migrations on an
installation whose schedule has been edited cannot resurrect the default.
"""
from datetime import time

from django.db import migrations


WEEKDAYS = range(0, 5)  # Monday .. Friday, matching date.weekday()
START = time(9, 0)
END = time(17, 0)
SLOT_MINUTES = 30


def seed(apps, schema_editor):
    AvailabilityRule = apps.get_model('kraios_admin', 'AvailabilityRule')

    if AvailabilityRule.objects.exists():
        return

    AvailabilityRule.objects.bulk_create([
        AvailabilityRule(
            weekday=weekday,
            start_time=START,
            end_time=END,
            slot_minutes=SLOT_MINUTES,
            is_active=True,
        )
        for weekday in WEEKDAYS
    ])


def unseed(apps, schema_editor):
    AvailabilityRule = apps.get_model('kraios_admin', 'AvailabilityRule')
    AvailabilityRule.objects.filter(
        weekday__in=list(WEEKDAYS),
        start_time=START,
        end_time=END,
        slot_minutes=SLOT_MINUTES,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('kraios_admin', '0002_backfill_meetings_from_signups'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
