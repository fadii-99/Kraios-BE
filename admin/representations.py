"""
Response builders for the admin console API.

The project validates INPUT with DRF serializers (``serializers.py``) and
builds OUTPUT with the plain functions here. The split is deliberate: every
record the console reads is a join across a database row, the placeholder
subscription store and a computed usage figure, and expressing that through
declarative serializer fields would hide the joins rather than document them.

Each builder assembles its dict from an explicit field list. Nothing dumps a
model and pops what should not be there - a field is exposed because it is
named here, which is what stops the next added column leaking by default.

Every builder takes already-loaded data. None of them issue a query, so a list
endpoint cannot become N+1 by adding a field to a row.
"""


def _iso_date(value):
    """A date or datetime as ``YYYY-MM-DD``; ``None`` stays ``None``."""
    if value is None:
        return None
    return value.date().isoformat() if hasattr(value, 'date') else value.isoformat()


def _iso_instant(value):
    return value.isoformat() if value is not None else None


def serialize_admin_profile(admin_profile):
    """The signed-in administrator, as the console's session needs it."""
    return {
        'id': str(admin_profile.pk),
        'name': admin_profile.user.full_name or admin_profile.user.email,
        'email': admin_profile.user.email,
        'role': admin_profile.display_role,
        'roleKey': admin_profile.role,
        'isPrivileged': admin_profile.is_privileged,
        'lastLoginAt': _iso_instant(admin_profile.last_login_at),
    }


def serialize_user(
    user,
    *,
    latest_meeting=None,
    signup_request=None,
    usage=None,
    subscription=None,
    usage_percent=0,
    setup_state='',
    signup_status='',
    meeting_status='',
):
    """One customer account, as every list row and the account page read it."""
    return {
        'id': str(user.pk),
        'name': user.full_name,
        'firm': user.firm_name,
        'email': user.email,
        'country': user.country,
        'jobTitle': user.job_title,
        'phone': user.phone,
        'signupDate': _iso_date(
            signup_request.created_at if signup_request else user.date_joined
        ),
        'signupStatus': signup_status,
        'accountStatus': 'Active' if user.is_active else 'Inactive',
        'accountSetup': setup_state,
        'lastActive': _iso_instant(user.last_login),
        'meetingStatus': meeting_status,
        'subscription': (subscription or {}).get('plan') or 'None',
        'subscriptionDetail': subscription,
        'usage': usage or {},
        'usagePercent': usage_percent,
        # Whether an administrator can issue credentials right now. The console
        # would otherwise have to re-derive this from three other fields.
        'hasPassword': user.has_usable_password(),
        'notes': latest_meeting.notes if latest_meeting else '',
    }


def serialize_meeting(meeting, *, include_user=True):
    """
    One onboarding meeting.

    ``date`` and ``time`` are served separately because the console sorts and
    filters on them separately; ``scheduledAt`` carries the same instant for
    anything that needs to compute with it. A meeting with no agreed slot has
    all three as ``None`` rather than a placeholder date.
    """
    scheduled_at = meeting.scheduled_at

    payload = {
        'id': str(meeting.pk),
        'date': _iso_date(scheduled_at),
        'time': scheduled_at.strftime('%H:%M') if scheduled_at else None,
        'scheduledAt': _iso_instant(scheduled_at),
        'durationMinutes': meeting.duration_minutes,
        'status': meeting.get_status_display(),
        'statusKey': meeting.status,
        'outcome': meeting.get_outcome_display(),
        'outcomeKey': meeting.outcome,
        'requestedSlot': meeting.requested_slot_label,
        'notes': meeting.notes,
        'createdDate': _iso_date(meeting.created_at),
        'completedAt': _iso_instant(meeting.completed_at),
        'cancelledAt': _iso_instant(meeting.cancelled_at),
        'reminderSentAt': _iso_instant(meeting.user_reminder_sent_at),
    }

    if include_user:
        payload.update({
            'userId': str(meeting.user_id),
            'user': meeting.user.full_name,
            'firm': meeting.user.firm_name,
            'email': meeting.user.email,
        })

    return payload


def serialize_plan(plan):
    """One subscription plan from the placeholder catalogue."""
    return {
        'id': plan['id'],
        'name': plan['name'],
        'description': plan['description'],
        'price': plan['price'],
        'billingCycle': plan['billingCycle'],
        'status': plan['status'],
        'projectLimit': plan['projectLimit'],
        'limit2d': plan['limit2d'],
        'limit3d': plan['limit3d'],
        'boqLimit': plan['boqLimit'],
        'documentLimit': plan['documentLimit'],
        'apiLimit': plan['apiLimit'],
        'storageGb': plan['storageGb'],
        'features': plan.get('features', []),
        'subscribers': plan.get('subscribers', 0),
    }


# ---------------------------------------------------------------------------
# Customer-facing views of the same records
#
# The console and the customer read the SAME plan rows, and they must not read
# the same FIELDS. Everything a customer is shown here is something they are
# buying; everything withheld is either an internal control or a number that
# would be a lie if printed.
# ---------------------------------------------------------------------------

def serialize_customer_plan(plan):
    """
    One plan as a CUSTOMER may see it.

    THREE FIELDS ARE DELIBERATELY ABSENT, and each for its own reason:

    ``status`` is an administrative switch. A customer is only ever offered
    Active plans (the view filters on it), so publishing the flag would say
    nothing and invite a UI that renders "Inactive" to somebody who cannot act
    on it.

    ``subscribers`` is business intelligence - how many accounts are on each
    plan. That belongs to whoever runs the business, not to the people being
    counted.

    ``apiLimit`` is a cap on something that does not exist. Nothing meters API
    requests, so printing "2,000 API requests" on a pricing card would be a
    promise the product cannot keep. It comes back the day there is an API to
    count.
    """
    return {
        'id': plan['id'],
        'name': plan['name'],
        'description': plan['description'],
        'price': plan['price'],
        'billingCycle': plan['billingCycle'],
        'features': plan.get('features', []),
        # The caps the customer is actually measured against, named the way the
        # usage metrics are, so a plan card and a usage meter can never disagree
        # about which number is which.
        'limits': {
            'projects': plan['projectLimit'],
            'plans2d': plan['limit2d'],
            'renders3d': plan['limit3d'],
            'boqs': plan['boqLimit'],
            'documents': plan['documentLimit'],
            'storageGb': plan['storageGb'],
        },
    }


def serialize_customer_subscription(subscription):
    """
    The account's OWN subscription, or ``None`` when it has none.

    ``None`` is a real answer and the one the UI has to be able to draw. An
    account that an administrator has not put on a plan HAS no plan, and a
    billing page that invents one - a name, a price, a renewal date - is
    telling somebody something false about their own account.

    ``status`` is the resolved one (``Active``, ``Past Due``, ``Cancelled``),
    not the stored state, so an activation whose end date has passed says so
    here exactly as it does in the console.
    """
    if not subscription:
        return None

    return {
        'planId': subscription.get('planId'),
        'plan': subscription.get('plan'),
        'billingCycle': subscription.get('billingCycle'),
        'price': subscription.get('price'),
        'status': subscription.get('status'),
        'startDate': subscription.get('startDate'),
        'renewalDate': subscription.get('renewalDate'),
    }


def serialize_usage_row(user, *, usage, limits, usage_percent, subscription):
    """One account's meters, for the Usage table."""
    return {
        'id': str(user.pk),
        'name': user.full_name,
        'firm': user.firm_name,
        'email': user.email,
        'accountStatus': 'Active' if user.is_active else 'Inactive',
        'lastActive': _iso_instant(user.last_login),
        'subscription': (subscription or {}).get('plan') or 'None',
        'usage': usage,
        'limits': limits,
        'usagePercent': usage_percent,
    }


def serialize_support_request(row, *, account_id=None):
    """
    One contact-form submission as the console reads it.

    `country` and `topic` are read with ``.get``, not indexed: rows filed
    before the contact form asked for them are still in the store, and a
    console that 500s on an old record is worse than one that shows a blank.
    """
    return {
        'id': row['id'],
        'name': row['name'],
        'email': row['email'],
        'firm': row['firm'],
        'country': row.get('country') or '',
        'topic': row.get('topic') or '',
        'subject': row['subject'],
        'message': row['message'],
        'submittedAt': row['submittedAt'],
        'status': row['status'],
        'priority': row['priority'],
        'assignee': row.get('assignee') or 'Unassigned',
        'accountId': account_id,
    }


def serialize_availability_rule(rule):
    return {
        'id': str(rule.pk),
        'weekday': rule.weekday,
        'weekdayLabel': rule.get_weekday_display(),
        'startTime': rule.start_time.strftime('%H:%M'),
        'endTime': rule.end_time.strftime('%H:%M'),
        'slotMinutes': rule.slot_minutes,
        'isActive': rule.is_active,
    }


def serialize_blackout(blackout):
    return {
        'id': str(blackout.pk),
        'date': blackout.date.isoformat(),
        'reason': blackout.reason,
    }


def serialize_audit_entry(entry):
    return {
        'id': str(entry.pk),
        'adminEmail': entry.admin_email,
        'action': entry.action,
        'targetType': entry.target_type,
        'targetId': entry.target_id,
        'summary': entry.summary,
        'metadata': entry.metadata,
        'ipAddress': entry.ip_address,
        'createdAt': _iso_instant(entry.created_at),
    }
