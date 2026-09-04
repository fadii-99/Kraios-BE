"""
Platform usage, computed from the records that actually exist.

Every figure here is derived from the project tables rather than stored in a
counter, so a number on the console cannot drift away from the thing it counts.
The cost of that choice is one aggregate query per metric; the alternative -
counters incremented at write time - is a set of numbers that quietly go wrong
after the first failed transaction and are never noticed.

The aggregates are grouped by owner in a single pass each, so the query count
is FIXED (seven) whether the console is showing one account or a thousand.
Doing it per user would have been eight queries per row.

``apiRequests`` is the one metric with no source: nothing meters public API
calls yet, so it is reported as zero rather than invented. It is left in the
vocabulary because the console renders a column for it and a metric that
appears and disappears is worse than one that honestly reads nothing.
"""
import logging

from django.db.models import Count, Sum

from projects.models import (
    BOQVersion,
    FloorPlanVersion,
    ProcessingJob,
    Project,
    ProjectAsset,
    ProjectDocument,
    ThreeDVersion,
)


logger = logging.getLogger(__name__)

BYTES_PER_GB = 1024 ** 3

# The metrics the platform meters, in the order the console prints them.
# `limit` names the matching allowance on a plan, or None where the metric is
# uncapped. One list, so a metric added here appears in the KPI row, the table,
# the per-account panel and the plan form rather than in three of them.
USAGE_METRICS = (
    {'key': 'projects', 'label': 'Projects', 'limit': 'projectLimit', 'unit': 'count'},
    {'key': 'plans2d', 'label': '2D Plans', 'limit': 'limit2d', 'unit': 'count'},
    {'key': 'renders3d', 'label': '3D Renders', 'limit': 'limit3d', 'unit': 'count'},
    {'key': 'boqs', 'label': 'BoQs', 'limit': 'boqLimit', 'unit': 'count'},
    {'key': 'aiRequests', 'label': 'AI Requests', 'limit': None, 'unit': 'count'},
    {'key': 'documents', 'label': 'Documents', 'limit': 'documentLimit', 'unit': 'count'},
    {'key': 'apiRequests', 'label': 'API Requests', 'limit': 'apiLimit', 'unit': 'count'},
    {'key': 'storageGb', 'label': 'Storage', 'limit': 'storageGb', 'unit': 'GB'},
)

EMPTY_USAGE = {metric['key']: 0 for metric in USAGE_METRICS}

# The bands at which the console tells an administrator to look at an account.
USAGE_NEAR_LIMIT_PERCENT = 80


def _counts_by_owner(queryset, owner_field):
    return {
        row[owner_field]: row['total']
        for row in queryset.values(owner_field).annotate(total=Count('id'))
    }


def usage_by_user(user_ids=None):
    """
    ``{user_id: {metric_key: value}}`` for the given users, or for everyone.

    Users with no activity are absent from the returned map; callers fall back
    to ``EMPTY_USAGE``.
    """
    projects = Project.objects.all()
    floor_plans = FloorPlanVersion.objects.all()
    renders = ThreeDVersion.objects.all()
    boqs = BOQVersion.objects.all()
    documents = ProjectDocument.objects.all()
    assets = ProjectAsset.objects.all()
    jobs = ProcessingJob.objects.all()

    if user_ids is not None:
        user_ids = list(user_ids)
        projects = projects.filter(owner_id__in=user_ids)
        floor_plans = floor_plans.filter(project__owner_id__in=user_ids)
        renders = renders.filter(project__owner_id__in=user_ids)
        boqs = boqs.filter(project__owner_id__in=user_ids)
        documents = documents.filter(project__owner_id__in=user_ids)
        assets = assets.filter(project__owner_id__in=user_ids)
        jobs = jobs.filter(project__owner_id__in=user_ids)

    # A failed generation consumed no allowance, so it is not counted. A queued
    # or processing one is counted: the work has been asked for, and an account
    # that could queue past its limit by leaving jobs unfinished is an account
    # with no limit.
    failed = ProcessingJob.FAILED
    floor_plans = floor_plans.exclude(status=failed)
    renders = renders.exclude(status=failed)
    boqs = boqs.exclude(status=failed)

    project_counts = _counts_by_owner(projects, 'owner_id')
    plan_counts = _counts_by_owner(floor_plans, 'project__owner_id')
    render_counts = _counts_by_owner(renders, 'project__owner_id')
    boq_counts = _counts_by_owner(boqs, 'project__owner_id')
    document_counts = _counts_by_owner(documents, 'project__owner_id')
    job_counts = _counts_by_owner(jobs, 'project__owner_id')

    storage_bytes = {
        row['project__owner_id']: row['total'] or 0
        for row in assets.values('project__owner_id').annotate(total=Sum('size'))
    }

    owners = set()
    for source in (
        project_counts,
        plan_counts,
        render_counts,
        boq_counts,
        document_counts,
        job_counts,
        storage_bytes,
    ):
        owners.update(source.keys())
    owners.discard(None)

    usage = {}
    for owner_id in owners:
        usage[owner_id] = {
            'projects': project_counts.get(owner_id, 0),
            'plans2d': plan_counts.get(owner_id, 0),
            'renders3d': render_counts.get(owner_id, 0),
            'boqs': boq_counts.get(owner_id, 0),
            'aiRequests': job_counts.get(owner_id, 0),
            'documents': document_counts.get(owner_id, 0),
            # Not metered yet - see the module docstring.
            'apiRequests': 0,
            'storageGb': round(storage_bytes.get(owner_id, 0) / BYTES_PER_GB, 2),
        }

    return usage


def plan_limits(plan):
    """The seven caps a plan publishes, keyed by usage metric."""
    if not plan:
        return None

    return {
        'projects': plan.get('projectLimit'),
        'plans2d': plan.get('limit2d'),
        'renders3d': plan.get('limit3d'),
        'boqs': plan.get('boqLimit'),
        'documents': plan.get('documentLimit'),
        'apiRequests': plan.get('apiLimit'),
        'storageGb': plan.get('storageGb'),
    }


def metric_usage(usage, metric, plan):
    """
    One metric's used / limit / remaining / percent.

    ``limit`` is None when the metric is uncapped, and then ``percent`` is None
    too rather than 0 - a meter drawn at 0% for an uncapped metric reads as
    "nothing used", which is the opposite of what is true.
    """
    used = (usage or {}).get(metric['key'], 0)
    raw_limit = plan.get(metric['limit']) if (metric['limit'] and plan) else None
    capped = isinstance(raw_limit, (int, float)) and raw_limit > 0

    return {
        'key': metric['key'],
        'label': metric['label'],
        'unit': metric['unit'],
        'used': used,
        'limit': raw_limit if capped else None,
        'remaining': max(0, raw_limit - used) if capped else None,
        'percent': round(used / raw_limit * 100) if capped else None,
    }


def usage_breakdown(usage, plan):
    """Every metric for one account, in the vocabulary's order."""
    return [metric_usage(usage, metric, plan) for metric in USAGE_METRICS]


def overall_usage_percent(usage, plan):
    """
    One number for "how full is this account".

    It is the HIGHEST capped metric, not the average. An account that has burnt
    its whole 3D allowance and nothing else is an account about to hit a wall,
    and averaging that away to 20% is how a console fails to warn anybody.
    """
    percents = [
        row['percent']
        for row in usage_breakdown(usage, plan)
        if row['percent'] is not None
    ]
    if not percents:
        return 0
    return min(200, max(percents))


def total_storage_gb():
    """Platform-wide stored bytes, in GB. One query."""
    total = ProjectAsset.objects.aggregate(total=Sum('size'))['total'] or 0
    return round(total / BYTES_PER_GB, 2)
