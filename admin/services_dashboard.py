"""
The admin overview.

Every figure is DERIVED from the same records the other pages manage, so
deactivating an account moves the Active Users tile and completing a meeting
moves the meetings tile. A dashboard whose numbers do not move when the data
does is a picture of a dashboard.

Windowed versus not, and why it matters: the account counts (total, active,
inactive, pending) are a state TODAY, not an event inside a period, so the
range control does not touch them. Only the trends and the volume tiles
re-slice. Windowing a state is how an overview starts contradicting the page it
links to.

What is real and what is not, stated plainly so nobody reads a placeholder as a
measurement:

* real, from the database - user counts, signups, meetings, projects,
  generation volume, storage, per-account usage;
* placeholder, from ``dummy_data`` - the plan catalogue, per-user
  subscriptions and the support queue.
"""
import logging
from datetime import timedelta

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from projects.models import BOQVersion, FloorPlanVersion, ProcessingJob, Project, ThreeDVersion

from . import dummy_data
from .listing import RANGE_DAYS
from .models import AdminAuditLog, Meeting
from .services_usage import (
    USAGE_NEAR_LIMIT_PERCENT,
    overall_usage_percent,
    total_storage_gb,
    usage_by_user,
)
from .services_users import customer_queryset


logger = logging.getLogger(__name__)

# Beyond this many days a daily series is bucketed by month. 365 marks on an
# axis a few hundred pixels wide is unreadable and slow to draw, and a month is
# also how somebody actually reads a year.
MONTHLY_BUCKET_THRESHOLD_DAYS = 120

# How far ahead the overview looks for meetings that need attention.
UPCOMING_MEETING_DAYS = 7

# How many entries each panel carries. Small on purpose: the overview says
# which page to open, it is not a smaller copy of that page.
ACTIVITY_LIMIT = 7
UPCOMING_LIMIT = 5
AT_RISK_LIMIT = 4


def _daily_counts(queryset, field):
    """``{date: count}`` for a queryset, bucketed by day in one query."""
    return {
        row['bucket']: row['total']
        for row in queryset.annotate(bucket=TruncDate(field))
        .values('bucket')
        .annotate(total=Count('id'))
        if row['bucket'] is not None
    }


def _series(counts, start_date, days, monthly):
    """
    A dense ``[{label, value}]`` series - every day present, gaps as zero.

    A sparse series drawn as a line implies the platform was idle on the days
    it omits, when in fact nothing was recorded for them; filling explicitly is
    what makes the two the same statement.
    """
    points = []
    for offset in range(days):
        day = start_date + timedelta(days=offset)
        points.append({'label': day.isoformat(), 'value': counts.get(day, 0)})

    if not monthly:
        return points

    buckets = {}
    for point in points:
        month = point['label'][:7]
        bucket = buckets.setdefault(month, {'label': f'{month}-01', 'value': 0})
        bucket['value'] += point['value']
    return list(buckets.values())


def _trend(current, previous, noun):
    """The line under a tile: a direction plus the words, never a bare colour."""
    if not previous:
        return None

    delta = round((current - previous) / previous * 100, 1)
    if abs(delta) < 0.1:
        return {'direction': 'flat', 'label': f'Level {noun}'}

    sign = '+' if delta > 0 else ''
    return {
        'direction': 'up' if delta > 0 else 'down',
        'label': f'{sign}{delta}% {noun}',
    }


def _count_in_window(queryset, field, start, end):
    return queryset.filter(**{f'{field}__gte': start, f'{field}__lt': end}).count()


def build_dashboard_overview(range_key):
    """The whole overview for one date window, as one payload."""
    days = RANGE_DAYS.get(range_key, RANGE_DAYS['30d'])
    now = timezone.now()
    window_start = now - timedelta(days=days)
    previous_start = now - timedelta(days=days * 2)
    noun = f'vs previous {days} days'

    users = customer_queryset()
    total_users = users.count()
    active_users = users.filter(is_active=True).count()

    # "Somewhere in onboarding" is an account that has never signed in and has
    # not been turned away. It is a live definition rather than a stored flag,
    # so it cannot go stale.
    pending_users = (
        users.filter(last_login__isnull=True)
        .exclude(signup_request__status='REJECTED')
        .count()
    )

    horizon = now + timedelta(days=UPCOMING_MEETING_DAYS)
    upcoming = list(
        Meeting.objects.filter(
            status=Meeting.SCHEDULED,
            scheduled_at__gte=now,
            scheduled_at__lte=horizon,
        )
        .select_related('user')
        .order_by('scheduled_at')
    )

    subscriptions = dummy_data.all_subscriptions()
    active_subscriptions = [
        entry for entry in subscriptions.values() if entry['status'] == 'Active'
    ]
    plans = dummy_data.list_plans()
    active_plans = [plan for plan in plans if plan['status'] == 'Active']

    projects_now = _count_in_window(Project.objects, 'created_at', window_start, now)
    projects_before = _count_in_window(
        Project.objects, 'created_at', previous_start, window_start
    )

    jobs_now = _count_in_window(ProcessingJob.objects, 'created_at', window_start, now)
    jobs_before = _count_in_window(
        ProcessingJob.objects, 'created_at', previous_start, window_start
    )

    signups_now = _count_in_window(users, 'date_joined', window_start, now)
    signups_before = _count_in_window(users, 'date_joined', previous_start, window_start)

    metrics = [
        {
            'id': 'total-users',
            'label': 'Total Users',
            'value': f'{total_users:,}',
            'hint': 'All registered accounts',
            'trend': _trend(signups_now, signups_before, 'new signups'),
            'to': '/users',
        },
        {
            'id': 'active-users',
            'label': 'Active Users',
            'value': f'{active_users:,}',
            'hint': 'Accounts able to sign in',
            'trend': None,
            'to': '/users',
        },
        {
            'id': 'inactive-users',
            'label': 'Inactive Users',
            'value': f'{total_users - active_users:,}',
            'hint': 'Deactivated or not yet set up',
            'trend': None,
            'to': '/users',
        },
        {
            'id': 'pending-signups',
            'label': 'Pending Signups',
            'value': f'{pending_users:,}',
            'hint': 'Somewhere in onboarding',
            'trend': None,
            'to': '/users',
        },
        {
            'id': 'upcoming-meetings',
            'label': 'Upcoming Meetings',
            'value': f'{len(upcoming):,}',
            'hint': f'Booked in the next {UPCOMING_MEETING_DAYS} days',
            'trend': None,
            'to': '/meetings',
        },
        {
            'id': 'active-subscriptions',
            'label': 'Active Subscriptions',
            'value': f'{len(active_subscriptions):,}',
            'hint': f'Across {len(active_plans)} live plans',
            'trend': None,
            'to': '/subscriptions',
            # Flagged so the console can mark the tile as not yet a measurement.
            'placeholder': True,
        },
        {
            'id': 'total-projects',
            'label': 'Total Projects',
            'value': f'{projects_now:,}',
            'hint': f'Created in the last {days} days',
            'trend': _trend(projects_now, projects_before, noun),
            'to': '/usage',
        },
        {
            'id': 'api-requests',
            # Public API calls are not metered yet, so this tile reports the
            # generation jobs the platform actually ran rather than a zero.
            'label': 'AI Requests',
            'value': f'{jobs_now:,}',
            'hint': f'Generation jobs in the last {days} days',
            'trend': _trend(jobs_now, jobs_before, noun),
            'to': '/usage',
        },
    ]

    monthly = days > MONTHLY_BUCKET_THRESHOLD_DAYS
    start_date = (now - timedelta(days=days - 1)).date()

    signup_counts = _daily_counts(
        users.filter(date_joined__gte=window_start), 'date_joined'
    )

    generation_counts = {}
    for model, field in (
        (FloorPlanVersion, 'created_at'),
        (ThreeDVersion, 'created_at'),
        (BOQVersion, 'created_at'),
    ):
        for day, total in _daily_counts(
            model.objects.filter(created_at__gte=window_start), field
        ).items():
            generation_counts[day] = generation_counts.get(day, 0) + total

    return {
        'range': range_key,
        'days': days,
        'metrics': metrics,
        'activity': _build_activity(users),
        'usage': _build_usage_summary(subscriptions, plans, active_subscriptions),
        'trends': {
            'users': _series(signup_counts, start_date, days, monthly),
            'generation': _series(generation_counts, start_date, days, monthly),
        },
        'plans': _build_plan_mix(active_plans, subscriptions),
        'upcomingMeetings': [
            {
                'id': str(meeting.pk),
                'userId': str(meeting.user_id),
                'user': meeting.user.full_name,
                'firm': meeting.user.firm_name,
                'email': meeting.user.email,
                'date': meeting.scheduled_at.date().isoformat(),
                'time': meeting.scheduled_at.strftime('%H:%M'),
                'status': meeting.get_status_display(),
            }
            for meeting in upcoming[:UPCOMING_LIMIT]
        ],
        'atRisk': _build_at_risk(subscriptions, plans),
    }


def _build_usage_summary(subscriptions, plans, active_subscriptions):
    """
    Platform storage as a share of what the assigned plans allow.

    It is the honest denominator: the platform has no allowance of its own, but
    the accounts on it collectively do, and "68% of everything customers have
    been given" is the figure that says when capacity needs looking at.
    """
    plans_by_id = {plan['id']: plan for plan in plans}
    allowance = sum(
        plans_by_id.get(entry['planId'], {}).get('storageGb', 0) or 0
        for entry in subscriptions.values()
        if entry['status'] == 'Active'
    )
    used = total_storage_gb()

    return {
        'percent': round(used / allowance * 100) if allowance else 0,
        'used': f'{round(used):,}',
        'allowance': f'{round(allowance):,}',
        'unit': 'GB of storage',
        'period': f'Across {len(active_subscriptions)} subscribed accounts',
    }


def _build_plan_mix(active_plans, subscriptions):
    counts = {}
    for entry in subscriptions.values():
        if entry['status'] == 'Active':
            counts[entry['planId']] = counts.get(entry['planId'], 0) + 1

    mix = [
        {
            'id': plan['id'],
            'name': plan['name'],
            'price': plan['price'],
            'billingCycle': plan['billingCycle'],
            'subscribers': counts.get(plan['id'], 0),
        }
        for plan in active_plans
    ]
    return sorted(mix, key=lambda row: row['subscribers'], reverse=True)


def _build_at_risk(subscriptions, plans):
    """The accounts closest to their allowance - the overview's one alert."""
    if not subscriptions:
        return []

    plans_by_id = {plan['id']: plan for plan in plans}
    user_ids = [int(user_id) for user_id in subscriptions if str(user_id).isdigit()]
    usage_map = usage_by_user(user_ids)

    rows = []
    for user in customer_queryset().filter(pk__in=user_ids).only(
        'id', 'full_name', 'firm_name'
    ):
        entry = subscriptions.get(str(user.pk))
        plan = plans_by_id.get(entry['planId']) if entry else None
        percent = overall_usage_percent(usage_map.get(user.pk, {}), plan)
        if percent >= USAGE_NEAR_LIMIT_PERCENT:
            rows.append({
                'id': str(user.pk),
                'name': user.full_name,
                'firm': user.firm_name,
                'percent': percent,
            })

    return sorted(rows, key=lambda row: row['percent'], reverse=True)[:AT_RISK_LIMIT]


def _build_activity(users):
    """
    The activity strip, derived from records rather than written down.

    Every line has a record behind it, which is what lets each row be a real
    link. ``date`` carries the instant and ``meta`` the context; the console
    puts them together, because baking a formatted date into ``meta`` would
    leave the strip unable to say "2 days ago" without parsing its own string.
    """
    entries = []

    for user in users.order_by('-date_joined')[:3]:
        entries.append({
            'id': f'act-signup-{user.pk}',
            'kind': 'signup',
            'label': f'{user.full_name} signed up',
            'meta': user.firm_name,
            'date': user.date_joined.isoformat(),
            'to': f'/users/{user.pk}',
        })

    booked = (
        Meeting.objects.filter(status=Meeting.SCHEDULED, scheduled_at__isnull=False)
        .select_related('user')
        .order_by('-created_at')[:2]
    )
    for meeting in booked:
        entries.append({
            'id': f'act-meeting-{meeting.pk}',
            'kind': 'meeting',
            'label': f'Onboarding call booked with {meeting.user.full_name}',
            'meta': meeting.user.firm_name,
            'date': meeting.created_at.isoformat(),
            'to': '/meetings',
        })

    # Subscription assignments are read from the audit trail, because the
    # assignment itself lives in the placeholder store and carries no history.
    assignments = AdminAuditLog.objects.filter(action='subscription.assign')[:2]
    for entry in assignments:
        entries.append({
            'id': f'act-sub-{entry.pk}',
            'kind': 'subscription',
            'label': entry.summary,
            'meta': entry.admin_email,
            'date': entry.created_at.isoformat(),
            'to': f'/users/{entry.target_id}' if entry.target_id else '/subscriptions',
        })

    for request in dummy_data.list_support_requests():
        if request['status'] != 'New':
            continue
        entries.append({
            'id': f'act-support-{request["id"]}',
            'kind': 'support',
            'label': f'New support request: {request["subject"]}',
            'meta': request['firm'],
            'date': request['submittedAt'],
            'to': f'/support/{request["id"]}',
        })
        if sum(1 for row in entries if row['kind'] == 'support') >= 2:
            break

    return sorted(entries, key=lambda row: row['date'], reverse=True)[:ACTIVITY_LIMIT]
