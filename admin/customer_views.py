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
