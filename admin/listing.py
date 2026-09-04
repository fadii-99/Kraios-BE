"""
Search, filter, sort and pagination rules shared by every admin list endpoint.

They live in one module for the same reason the console keeps its own copies in
one module: a filter re-derived per endpoint drifts, and the drift is where a
list starts showing rows it should not.

Sorting is ALLOWLISTED. A caller names a sort key from a fixed map and the map
supplies the ORM expression, so a query parameter can never reach a column the
endpoint did not intend to expose or order by a related field that fans the
query out.

Every list endpoint paginates. An unbounded list is a latency incident waiting
for the first large account, so ``page_size`` is clamped rather than trusted.
"""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone


# Matches the console's own "no filter" sentinel, so an untouched dropdown and
# an absent parameter mean the same thing.
ALL = 'all'

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# Coarse windows a table filter offers, in days counted back from today
# inclusive. Kept here rather than in each view so "This Week" cannot mean
# seven days on one page and a calendar week on another.
DATE_WINDOWS = {
    'today': 1,
    'week': 7,
    'month': 30,
}

# Trend windows a chart offers.
RANGE_DAYS = {
    '7d': 7,
    '30d': 30,
    '90d': 90,
    '12m': 365,
}
DEFAULT_RANGE = '30d'

def parse_int(raw, default, minimum=None, maximum=None):
    """Guarded integer conversion; unparsable input falls back, never raises."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = default

    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def read_filter(request, name):
    """A filter value, or ``None`` when the caller did not narrow anything."""
    raw = request.query_params.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    if not raw or raw == ALL:
        return None
    return raw


def read_range(request, name='range'):
    """A trend window key from ``RANGE_DAYS``, defaulting rather than erroring."""
    requested = (request.query_params.get(name) or '').strip()
    return requested if requested in RANGE_DAYS else DEFAULT_RANGE


def read_page_params(request):
    page = parse_int(request.query_params.get('page'), 1, minimum=1)
    page_size = parse_int(
        request.query_params.get('page_size'),
        DEFAULT_PAGE_SIZE,
        minimum=1,
        maximum=MAX_PAGE_SIZE,
    )
    return page, page_size


def search_filter(query, fields):
    """
    A case-insensitive substring match across ``fields``, as one ``Q``.

    Which fields are searched is always the caller's decision: including a
    country column would make "in" match half a table through "United Kingdom"
    and "India", and a search that returns everything is worse than one that
    returns nothing.
    """
    needle = (query or '').strip()
    if not needle:
        return Q()

    matches = Q()
    for field in fields:
        matches |= Q(**{f'{field}__icontains': needle})
    return matches


def window_start(window_key, now=None):
    """
    The inclusive lower bound of a coarse date window, or ``None``.

    A record with no date is outside every window rather than swept into all of
    them, which is what "Today" has to mean to be worth anything. Callers
    enforce that by filtering on a non-null column.
    """
    days = DATE_WINDOWS.get(window_key)
    if not days:
        return None

    now = now or timezone.now()
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_of_today - timedelta(days=days - 1)


def apply_sort(queryset, requested_key, requested_direction, allowed, default_key):
    """
    Order a queryset by an allowlisted key.

    ``allowed`` maps a caller-facing key to the ORM field (or tuple of fields)
    it sorts by. An unknown key falls back to ``default_key`` rather than
    erroring: a stale bookmark should still render a list.
    """
    key = requested_key if requested_key in allowed else default_key
    descending = str(requested_direction or '').lower() == 'desc'

    fields = allowed[key]
    if isinstance(fields, str):
        fields = (fields,)

    ordered = []
    for field in fields:
        ordered.append(f'-{field}' if descending else field)

    # A stable tiebreaker, so two rows with the same sort value do not swap
    # places between pages and hide a record from the pager entirely.
    ordered.append('-pk' if descending else 'pk')
    return queryset.order_by(*ordered)


def sort_rows(rows, requested_key, requested_direction, allowed, default_key):
    """``apply_sort`` for the in-memory placeholder lists."""
    key = requested_key if requested_key in allowed else default_key
    descending = str(requested_direction or '').lower() == 'desc'
    field = allowed[key]

    def sort_value(row):
        value = row.get(field)
        # None sorts last in both directions: an absent value is not "the
        # smallest one", and burying the rows that have no date under a
        # descending sort is how a table hides what somebody was looking for.
        if value is None:
            return (1, '')
        if isinstance(value, bool):
            return (0, int(value))
        if isinstance(value, (int, float)):
            return (0, value)
        return (0, str(value).lower())

    ordered = sorted(rows, key=sort_value, reverse=descending)
    return ordered


def pagination_envelope(page, page_size, total_items):
    total_pages = max(1, -(-total_items // page_size))
    current = min(page, total_pages)
    return {
        'page': current,
        'page_size': page_size,
        'total_items': total_items,
        'total_pages': total_pages,
        'has_next': current < total_pages,
        'has_previous': current > 1,
    }


def paginate_queryset(queryset, page, page_size):
    """Return ``(rows, pagination)`` for a queryset, with one COUNT query."""
    total_items = queryset.count()
    pagination = pagination_envelope(page, page_size, total_items)
    offset = (pagination['page'] - 1) * page_size
    return list(queryset[offset:offset + page_size]), pagination


def paginate_rows(rows, page, page_size):
    """``paginate_queryset`` for an already-materialised list."""
    pagination = pagination_envelope(page, page_size, len(rows))
    offset = (pagination['page'] - 1) * page_size
    return rows[offset:offset + page_size], pagination


def list_response(items, pagination):
    """The one list envelope every admin endpoint returns."""
    return {'items': items, 'pagination': pagination}
