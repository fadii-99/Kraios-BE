"""
Placeholder store for subscription plans, per-user subscriptions and support.

WHY THIS EXISTS AND WHAT IT IS NOT
----------------------------------
There is no payment gateway yet and the plan catalogue is not settled, so the
brief is explicit: no database schema for plans and no database relation
between a user and a plan. This module honours that. Nothing here is a Django
model, nothing here appears in a migration, and deleting the JSON file named by
``KRAIOS_ADMIN_DUMMY_STORE_PATH`` returns the console to its seeded state
without touching the database.

It is file-backed rather than in-memory because an assignment an administrator
made before lunch has to still be there afterwards. A process-local dict would
have made "activate this account on Pro for 45 days" a lie the moment a worker
restarted.

CONCURRENCY. Every mutation is a read-modify-write under an exclusive lock: a
thread lock for the workers inside one process, and ``flock`` on the data file
for the several processes ``compose.yaml`` runs. The write itself goes to a
temporary file and is then renamed, so a crash mid-write cannot leave a
half-serialised store behind.

THIS IS NOT A DATABASE. It is single-node, it has no transactions across
operations, and it will not scale past a handful of writers. When the plan
model lands, replace the bodies of the accessor functions below with ORM calls;
every caller reads plain dictionaries and will not change.
"""
import fcntl
import json
import logging
import os
import tempfile
import threading
import uuid
from datetime import date, datetime, timedelta
from datetime import timezone as datetime_timezone

from django.conf import settings


logger = logging.getLogger(__name__)

_process_lock = threading.RLock()

STORE_VERSION = 1

# Plan statuses and billing cycles the console offers. Mirrored from the
# admin console's own vocabulary so a filter can never offer a value no record
# can hold.
PLAN_STATUSES = ('Active', 'Inactive')
BILLING_CYCLES = ('Monthly', 'Annual')

SUPPORT_STATUSES = ('New', 'Open', 'In Progress', 'Resolved', 'Closed')
SUPPORT_PRIORITIES = ('Low', 'Medium', 'High', 'Urgent')

# What the public contact form asks somebody to pick. A closed vocabulary
# rather than a free-text category, because the console FILTERS on it and a
# filter over free text is a filter over typos.
SUPPORT_TOPICS = (
    'Product demo',
    'Pricing and plans',
    'Technical issue',
    'Billing and invoices',
    'Partnerships',
    'Something else',
)

# The priority a request STARTS at, by topic.
#
# Derived from the topic rather than asked for, because a visitor-set priority
# is not a signal - given the choice, everything arrives Urgent. The topic is
# something the sender has no reason to misreport, and an administrator can
# change the priority from the console afterwards, which is where triage
# belongs. Anything not named here starts at DEFAULT_SUPPORT_PRIORITY.
_TOPIC_PRIORITY = {
    'Technical issue': 'High',
    'Billing and invoices': 'High',
}

DEFAULT_SUPPORT_PRIORITY = 'Medium'

# The default activation period an administrator gets when they do not name
# one, and the bounds a custom deal may set. Two years is an arbitrary ceiling
# that is still far beyond any deal anybody has described.
DEFAULT_SUBSCRIPTION_DAYS = 30
MIN_SUBSCRIPTION_DAYS = 1
MAX_SUBSCRIPTION_DAYS = 730

# An annual cycle is billed at twelve months less 15%, the same discount the
# public pricing page quotes. Computed rather than stored so the two cannot
# drift apart.
ANNUAL_DISCOUNT_MULTIPLIER = 0.85

# The numeric allowance fields every plan carries, and the usage metric each
# one caps. The console measures usage against these names.
PLAN_LIMIT_FIELDS = (
    ('projectLimit', 'Project limit'),
    ('limit2d', '2D limit'),
    ('limit3d', '3D limit'),
    ('boqLimit', 'BoQ limit'),
    ('documentLimit', 'Document limit'),
    ('apiLimit', 'API limit'),
    ('storageGb', 'Storage'),
)

# Limits a caller may leave out entirely.
#
# `apiLimit` is here because NOTHING METERS IT. There is no public API and
# `apiRequests` is 0 for every account, so the admin console has no field for
# it - and requiring one made every plan the console sent a 400, on create and
# on edit alike, for a cap that measures nothing. Absent means "carry the value
# the plan already had", or 0 on a new plan, which `metric_usage` reads as
# uncapped. A caller that DOES send it is still validated like any other limit,
# so the seeded values stay editable the day an API exists.
OPTIONAL_PLAN_LIMITS = frozenset({'apiLimit'})

_SEED_PLANS = [
    {
        'id': 'plan-starter',
        'name': 'Starter',
        'description': (
            'For a single practitioner running KRAIOS on a live project. '
            'The whole workflow, at a volume that suits one desk.'
        ),
        'price': 49,
        'billingCycle': 'Monthly',
        'projectLimit': 5,
        'limit2d': 40,
        'limit3d': 20,
        'boqLimit': 15,
        'documentLimit': 50,
        'apiLimit': 2000,
        'storageGb': 20,
        'features': ['2D floor plans', '3D rendering', 'BoQ export', 'Email support'],
        'status': 'Active',
    },
    {
        'id': 'plan-studio',
        'name': 'Studio',
        'description': (
            'For a working studio carrying several projects at once, with the '
            'shared library included.'
        ),
        'price': 189,
        'billingCycle': 'Monthly',
        'projectLimit': 30,
        'limit2d': 300,
        'limit3d': 180,
        'boqLimit': 120,
        'documentLimit': 400,
        'apiLimit': 25000,
        'storageGb': 200,
        'features': [
            '2D floor plans',
            '3D rendering',
            'BoQ export',
            'Shared project library',
            'Priority support',
        ],
        'status': 'Active',
    },
    {
        'id': 'plan-enterprise',
        'name': 'Enterprise',
        'description': (
            'For a multi-office practice - full generation volume and a named '
            'account contact.'
        ),
        'price': 690,
        'billingCycle': 'Monthly',
        'projectLimit': 250,
        'limit2d': 2500,
        'limit3d': 1500,
        'boqLimit': 1000,
        'documentLimit': 4000,
        'apiLimit': 250000,
        'storageGb': 2000,
        'features': [
            '2D floor plans',
            '3D rendering',
            'BoQ export',
            'Shared project library',
            'SSO',
            'Named account contact',
            'Onboarding programme',
        ],
        'status': 'Active',
    },
]


def _store_path():
    return str(settings.KRAIOS_ADMIN_DUMMY_STORE_PATH)


def _empty_store():
    return {
        'version': STORE_VERSION,
        'plans': [dict(plan) for plan in _SEED_PLANS],
        'subscriptions': {},
        'support': [],
    }


def _read_unlocked(path):
    if not os.path.exists(path):
        return _empty_store()

    with open(path, 'r', encoding='utf-8') as handle:
        raw = handle.read().strip()

    if not raw:
        return _empty_store()

    # A corrupt store is raised, not repaired. Silently re-seeding would throw
    # away every plan an administrator had created and look like a success.
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f'The admin placeholder store at {path} is not valid JSON.'
        ) from exc

    data.setdefault('version', STORE_VERSION)
    data.setdefault('plans', [])
    data.setdefault('subscriptions', {})
    data.setdefault('support', [])
    return data


def _write_unlocked(path, data):
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)

    handle, temporary_path = tempfile.mkstemp(dir=directory, suffix='.tmp')
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except Exception:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
        raise


class _StoreSession:
    """Exclusive read-modify-write access to the placeholder store."""

    def __init__(self):
        self.path = _store_path()
        self.lock_path = f'{self.path}.lock'
        self.data = None
        self._lock_handle = None

    def __enter__(self):
        _process_lock.acquire()
        try:
            os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
            self._lock_handle = open(self.lock_path, 'a+')
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX)
            self.data = _read_unlocked(self.path)
        except Exception:
            # An unwritable directory or a corrupt file must not leave the
            # process lock held - every later caller would block forever on a
            # failure that has already been reported once.
            if self._lock_handle is not None:
                self._lock_handle.close()
                self._lock_handle = None
            _process_lock.release()
            raise
        return self

    def save(self):
        _write_unlocked(self.path, self.data)

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if self._lock_handle is not None:
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
                self._lock_handle.close()
        finally:
            self._lock_handle = None
            _process_lock.release()
        return False


def _today():
    return date.today()


def _iso(value):
    return value.isoformat() if hasattr(value, 'isoformat') else value


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

def _plan_with_counts(plan, subscriptions):
    """A plan as the console reads it: the record plus its live account count."""
    subscribers = sum(
        1
        for entry in subscriptions.values()
        if entry.get('planId') == plan['id'] and entry.get('state') == 'ACTIVE'
    )
    return {**plan, 'subscribers': subscribers}


def list_plans():
    with _StoreSession() as session:
        return [
            _plan_with_counts(plan, session.data['subscriptions'])
            for plan in session.data['plans']
        ]


def get_plan(plan_id):
    with _StoreSession() as session:
        for plan in session.data['plans']:
            if plan['id'] == plan_id:
                return _plan_with_counts(plan, session.data['subscriptions'])
    return None


def validate_plan(payload, *, ignore_id=None, existing_plans=None):
    """
    The plan rules, returning ``{field: message}``. An empty dict means valid.

    Mirrors the console's own client-side rules so a field is marked before a
    submit, and enforces them again here because the form is not the only thing
    that can call this.
    """
    errors = {}
    plans = existing_plans
    if plans is None:
        with _StoreSession() as session:
            plans = list(session.data['plans'])

    name = str(payload.get('name') or '').strip()
    if not name:
        errors['name'] = 'A plan name is required'
    elif len(name) < 2:
        errors['name'] = 'A plan name needs at least two characters'
    elif len(name) > 80:
        errors['name'] = 'A plan name cannot be longer than 80 characters'
    elif any(
        plan['id'] != ignore_id and plan['name'].lower() == name.lower()
        for plan in plans
    ):
        errors['name'] = 'A plan with that name already exists'

    description = str(payload.get('description') or '').strip()
    if not description:
        errors['description'] = 'A description is required'
    elif len(description) > 500:
        errors['description'] = 'A description cannot be longer than 500 characters'

    price = payload.get('price')
    if price is None or price == '':
        errors['price'] = 'A price is required'
    else:
        try:
            numeric_price = float(price)
            if numeric_price < 0 or numeric_price > 1_000_000:
                errors['price'] = 'Price must be between 0 and 1,000,000'
        except (TypeError, ValueError):
            errors['price'] = 'Price must be a number'

    if payload.get('billingCycle') not in BILLING_CYCLES:
        errors['billingCycle'] = 'Choose a billing cycle'

    if payload.get('status') not in PLAN_STATUSES:
        errors['status'] = 'Choose a status'

    for field, label in PLAN_LIMIT_FIELDS:
        value = payload.get(field)
        if value is None or value == '':
            if field in OPTIONAL_PLAN_LIMITS:
                continue
            errors[field] = f'{label} is required'
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            errors[field] = f'{label} must be a number'
            continue
        if numeric < 0:
            errors[field] = f'{label} cannot be negative'
        elif numeric > 10_000_000:
            errors[field] = f'{label} is unreasonably large'
        elif field != 'storageGb' and numeric != int(numeric):
            errors[field] = f'{label} must be a whole number'

    return errors


def _normalise_plan(payload, current=None):
    """
    Turn a validated payload into the record the store holds.

    ``current`` is the plan being edited, and it is what an omitted
    ``OPTIONAL_PLAN_LIMITS`` field falls back to. Without it an edit would
    REPLACE a cap the caller never mentioned with 0 - so saving a plan from a
    console that has no API-limit field would quietly uncap its API limit. A
    field nobody sent must not be a field somebody changed.
    """
    features = payload.get('features') or []
    if isinstance(features, str):
        features = [line.strip() for line in features.splitlines()]

    record = {
        'name': str(payload['name']).strip(),
        'description': str(payload['description']).strip(),
        'price': round(float(payload['price']), 2),
        'billingCycle': payload['billingCycle'],
        'status': payload['status'],
        'features': [str(item).strip()[:120] for item in features if str(item).strip()][:20],
    }

    for field, _label in PLAN_LIMIT_FIELDS:
        raw = payload.get(field)
        if raw is None or raw == '':
            raw = (current or {}).get(field, 0)

        value = float(raw)
        record[field] = value if field == 'storageGb' else int(value)

    return record


def create_plan(payload):
    with _StoreSession() as session:
        errors = validate_plan(payload, existing_plans=session.data['plans'])
        if errors:
            return None, errors

        plan = {'id': f'plan-{uuid.uuid4().hex[:12]}', **_normalise_plan(payload)}
        session.data['plans'].append(plan)
        session.save()
        return _plan_with_counts(plan, session.data['subscriptions']), {}


def update_plan(plan_id, payload):
    with _StoreSession() as session:
        plan = next((row for row in session.data['plans'] if row['id'] == plan_id), None)
        if plan is None:
            return None, {'detail': 'That plan no longer exists.'}

        errors = validate_plan(
            payload,
            ignore_id=plan_id,
            existing_plans=session.data['plans'],
        )
        if errors:
            return None, errors

        plan.update(_normalise_plan(payload, current=plan))

        # A rename has to follow the accounts already on the plan, or those
        # subscriptions point at a plan name that exists nowhere.
        for entry in session.data['subscriptions'].values():
            if entry.get('planId') == plan_id:
                entry['plan'] = plan['name']
                entry['price'] = _cycle_price(plan['price'], entry.get('billingCycle'))

        session.save()
        return _plan_with_counts(plan, session.data['subscriptions']), {}


def set_plan_status(plan_id, status):
    if status not in PLAN_STATUSES:
        return None, {'status': 'Choose a status'}

    with _StoreSession() as session:
        plan = next((row for row in session.data['plans'] if row['id'] == plan_id), None)
        if plan is None:
            return None, {'detail': 'That plan no longer exists.'}

        plan['status'] = status
        session.save()
        return _plan_with_counts(plan, session.data['subscriptions']), {}


def delete_plan(plan_id):
    """
    Remove a plan, refusing while accounts are still on it.

    The refusal is not a nicety: deleting it anyway leaves those accounts
    pointing at a plan id that resolves to nothing, and every limit their usage
    is measured against silently disappears.
    """
    with _StoreSession() as session:
        plan = next((row for row in session.data['plans'] if row['id'] == plan_id), None)
        if plan is None:
            return None, {'detail': 'That plan no longer exists.'}

        subscribers = _plan_with_counts(plan, session.data['subscriptions'])['subscribers']
        if subscribers > 0:
            noun = 'account' if subscribers == 1 else 'accounts'
            return None, {
                'detail': (
                    f'{plan["name"]} still has {subscribers} {noun} on it. '
                    'Move them to another plan, or deactivate this one instead.'
                )
            }

        session.data['plans'] = [
            row for row in session.data['plans'] if row['id'] != plan_id
        ]
        session.save()
        return {'id': plan_id}, {}


# ---------------------------------------------------------------------------
# Per-user subscriptions
# ---------------------------------------------------------------------------

def _cycle_price(base_price, billing_cycle):
    if billing_cycle == 'Annual':
        return round(base_price * 12 * ANNUAL_DISCOUNT_MULTIPLIER, 2)
    return round(float(base_price), 2)


def _project_subscription(entry):
    """
    The subscription as the console reads it, with its status resolved against
    today.

    ``state`` is what is stored (ACTIVE or CANCELLED); ``status`` is what an
    administrator sees. An activation whose renewal date has passed reads as
    ``Past Due`` rather than staying ``Active`` — the console has no payment
    gateway to tell it otherwise, and an expired activation that still claims
    to be active is the one figure nobody may trust.
    """
    if entry is None:
        return None

    renewal = entry.get('renewalDate')
    expired = bool(renewal) and date.fromisoformat(renewal) < _today()

    if entry.get('state') == 'CANCELLED':
        status = 'Cancelled'
    elif expired:
        status = 'Past Due'
    else:
        status = 'Active'

    return {
        'planId': entry.get('planId'),
        'plan': entry.get('plan'),
        'billingCycle': entry.get('billingCycle'),
        'price': entry.get('price'),
        'status': status,
        'startDate': entry.get('startDate'),
        'renewalDate': renewal,
        'durationDays': entry.get('durationDays'),
        'isExpired': expired and entry.get('state') != 'CANCELLED',
        'assignedAt': entry.get('assignedAt'),
        'assignedBy': entry.get('assignedBy'),
    }


def get_subscription(user_id):
    with _StoreSession() as session:
        return _project_subscription(session.data['subscriptions'].get(str(user_id)))


def all_subscriptions():
    """``{user_id: projected subscription}`` for every account that has one."""
    with _StoreSession() as session:
        return {
            user_id: _project_subscription(entry)
            for user_id, entry in session.data['subscriptions'].items()
        }


def assign_subscription(user_id, plan_id, billing_cycle, duration_days, assigned_by=''):
    """
    Put one account on a plan for a fixed number of days.

    There is no payment gateway, so activation is an administrative act with an
    explicit end date rather than a recurring charge. ``duration_days`` defaults
    to 30 and is clamped, so a typo cannot grant a decade.
    """
    if billing_cycle not in BILLING_CYCLES:
        return None, {'billingCycle': 'Choose a billing cycle'}

    try:
        duration = int(duration_days or DEFAULT_SUBSCRIPTION_DAYS)
    except (TypeError, ValueError):
        return None, {'durationDays': 'Duration must be a whole number of days'}

    if duration < MIN_SUBSCRIPTION_DAYS or duration > MAX_SUBSCRIPTION_DAYS:
        return None, {
            'durationDays': (
                f'Duration must be between {MIN_SUBSCRIPTION_DAYS} and '
                f'{MAX_SUBSCRIPTION_DAYS} days'
            )
        }

    with _StoreSession() as session:
        plan = next((row for row in session.data['plans'] if row['id'] == plan_id), None)
        if plan is None:
            return None, {'planId': 'That plan no longer exists.'}
        if plan['status'] != 'Active':
            return None, {'planId': 'That plan is not active and cannot be assigned.'}

        start = _today()
        entry = {
            'planId': plan['id'],
            'plan': plan['name'],
            'billingCycle': billing_cycle,
            'price': _cycle_price(plan['price'], billing_cycle),
            'state': 'ACTIVE',
            'startDate': _iso(start),
            'renewalDate': _iso(start + timedelta(days=duration)),
            'durationDays': duration,
            'assignedAt': datetime.now(datetime_timezone.utc).isoformat(timespec='seconds'),
            'assignedBy': assigned_by,
        }
        session.data['subscriptions'][str(user_id)] = entry
        session.save()
        return _project_subscription(entry), {}


def clear_subscription(user_id):
    """Take an account off every plan. Returns True when something changed."""
    with _StoreSession() as session:
        removed = session.data['subscriptions'].pop(str(user_id), None)
        if removed is None:
            return False
        session.save()
        return True


# ---------------------------------------------------------------------------
# Support queue
# ---------------------------------------------------------------------------

def create_support_request(payload):
    """
    File one contact-form submission into the queue.

    THE ONLY WRITER THAT IS NOT AN ADMINISTRATOR. Everything else in this
    module is called by a signed-in console user acting on a record that
    already exists; this is called by an anonymous visitor, so it takes a
    payload that has already been validated and truncated by
    ``ContactRequestSerializer`` and adds nothing the sender can choose.

    ``status`` starts at New and ``priority`` is DERIVED from the topic - see
    ``_TOPIC_PRIORITY``. Neither is in the payload, and that is the point: a
    field a stranger can set is a field a stranger can set to Urgent.

    ``assignee`` starts empty rather than 'Unassigned' so the console's own
    triage rule - moving a request out of New assigns it - still has an unset
    value to recognise.
    """
    topic = payload.get('topic') or 'Something else'

    row = {
        'id': f'sup-{uuid.uuid4().hex[:12]}',
        'name': payload['name'],
        'email': payload['email'],
        'firm': payload.get('firm', ''),
        'country': payload.get('country', ''),
        'topic': topic,
        'subject': payload['subject'],
        'message': payload['message'],
        'submittedAt': datetime.now(datetime_timezone.utc).isoformat(timespec='seconds'),
        'status': 'New',
        'priority': _TOPIC_PRIORITY.get(topic, DEFAULT_SUPPORT_PRIORITY),
        'assignee': '',
    }

    with _StoreSession() as session:
        session.data['support'].append(row)
        session.save()

    return dict(row)


def list_support_requests():
    with _StoreSession() as session:
        return sorted(
            (dict(row) for row in session.data['support']),
            key=lambda row: row.get('submittedAt') or '',
            reverse=True,
        )


def get_support_request(request_id):
    with _StoreSession() as session:
        for row in session.data['support']:
            if row['id'] == request_id:
                return dict(row)
    return None


def update_support_request(request_id, *, status=None, priority=None, assignee=None):
    if status is not None and status not in SUPPORT_STATUSES:
        return None, {'status': 'Unsupported status'}
    if priority is not None and priority not in SUPPORT_PRIORITIES:
        return None, {'priority': 'Unsupported priority'}

    with _StoreSession() as session:
        row = next((item for item in session.data['support'] if item['id'] == request_id), None)
        if row is None:
            return None, {'detail': 'That support request no longer exists.'}

        if status is not None:
            row['status'] = status
            # Moving a request out of New without an owner leaves a ticket that
            # looks handled and is nobody's. Picking it up is what assignment
            # means, so the caller becomes the owner by default.
            if status != 'New' and not row.get('assignee'):
                row['assignee'] = 'Unassigned'

        if priority is not None:
            row['priority'] = priority

        if assignee is not None:
            row['assignee'] = str(assignee)[:120]

        session.save()
        return dict(row), {}


def expire_subscription_for_tests(user_id):
    """
    Backdate one activation's end date so it reads ``Past Due``. Tests only.

    Exists because the alternative is a test that reaches into the JSON file
    behind this module's back, and a test that knows the storage format is a
    test that breaks when the storage changes.
    """
    with _StoreSession() as session:
        entry = session.data['subscriptions'].get(str(user_id))
        if entry is None:
            return None
        entry['renewalDate'] = _iso(_today() - timedelta(days=1))
        session.save()
        return _project_subscription(entry)


def reset_store():
    """Drop every placeholder record back to the seeded state. Tests only."""
    with _StoreSession() as session:
        session.data = _empty_store()
        session.save()
