"""
What a SIGNED-IN CUSTOMER may read of the plan catalogue and of their own
subscription.

WHY THIS EXISTS. The catalogue an administrator edits and the pricing a
customer sees have to be one set of records, or the two drift and the product
starts quoting a price nobody set. Until now the customer dashboard read a
hardcoded module: three invented plans, in dollars, with an invented "Premium
Pro" activation shown to every account regardless of what it was actually on.
These two endpoints are what replace it.

WHY NOT IN ``views.py``. Same reason as ``public_views.py``: that module states
that every view in it is an administrator's, and a route added to
``admin/urls.py`` is either authenticated as one or is one of the four session
endpoints. A customer endpoint is a third thing. It lives here and is mounted
outside ``/api/v1/admin/`` so no customer's browser calls a path that says
"admin".

READ-ONLY, AND THAT IS THE WHOLE SURFACE. There is no payment gateway: an
account is put on a plan by an administrator, for a fixed number of days. So a
customer may LOOK at the catalogue and at what they are on, and there is
nothing here to change either. A "Choose plan" button that POSTed something
would be a checkout that does not exist.

WHAT IS WITHHELD is decided in ``representations.serialize_customer_plan`` -
plan status, subscriber counts and the unmetered API cap - and the filtering to
Active plans is done here, where the queryset is.
"""
import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import dummy_data, representations
from .services_usage import (
    EMPTY_USAGE,
    overall_usage_percent,
    usage_breakdown,
    usage_by_user,
)


logger = logging.getLogger(__name__)


class CustomerPlanListAPIView(APIView):
    """``GET`` the plans a customer may be put on."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        plans = [
            representations.serialize_customer_plan(plan)
            for plan in dummy_data.list_plans()
            if plan.get('status') == 'Active'
        ]

        # Cheapest first. The console sorts however an administrator asks; a
        # pricing page has one right order and it is not "whichever was created
        # first".
        plans.sort(key=lambda plan: (plan['price'], plan['name'].lower()))

        return Response({'plans': plans})


class CustomerSubscriptionAPIView(APIView):
    """``GET`` the signed-in account's own subscription, or ``null``."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        subscription = dummy_data.get_subscription(request.user.pk)

        return Response({
            'subscription': representations.serialize_customer_subscription(subscription),
        })


class CustomerUsageAPIView(APIView):
    """
    ``GET`` what this account has used, against what its plan allows.

    THE OTHER HALF OF A PLAN. The catalogue says an account may have 30
    projects; only this says it has used 11 of them. Without it a customer can
    read their allowance and has no way to know how much of it is left, which
    is the one billing question they actually ask.

    THE SAME NUMBERS THE CONSOLE SEES. ``usage_by_user`` and ``usage_breakdown``
    are the functions behind the admin Usage screen, called here with the
    caller's own id. There is no second count to drift: if the console says an
    account has generated 40 renders, so does this.

    ``limit`` and ``percent`` are ``None`` for an uncapped metric and for EVERY
    metric when the account has no plan — a meter drawn at 0% against a cap
    that does not exist reads as "nothing used", which is the opposite of what
    is true. The caller renders a count without a bar in that case.

    ``apiRequests`` is dropped for the same reason its cap is withheld from
    ``serialize_customer_plan``: nothing meters it. Publishing "0 of 25,000 API
    requests" beside a plan card that does not mention an API allowance would
    be the product promising something it has no way to count.
    """

    permission_classes = [IsAuthenticated]

    # Metrics a customer is not shown, by key. Kept as a set rather than
    # filtered inline so adding one is a decision somebody has to write down.
    HIDDEN_METRICS = frozenset({'apiRequests'})

    def get(self, request):
        user_id = request.user.pk

        usage = usage_by_user([user_id]).get(user_id, EMPTY_USAGE)

        subscription = dummy_data.get_subscription(user_id)
        plan = dummy_data.get_plan(subscription['planId']) if subscription else None

        return Response({
            'usage': [
                row
                for row in usage_breakdown(usage, plan)
                if row['key'] not in self.HIDDEN_METRICS
            ],
            'overallPercent': overall_usage_percent(usage, plan) if plan else None,
            # Stated rather than left to be inferred from null limits, so the
            # page can say "no plan" once instead of eight times.
            'hasPlan': plan is not None,
        })
