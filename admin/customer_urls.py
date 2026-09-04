"""
Billing routes for the SIGNED-IN CUSTOMER.

Mounted at ``/api/v1/billing/``, deliberately not under ``/api/v1/admin/``: the
records are the console's, the reader is not. Keeping the prefixes apart is
what lets ``admin/urls.py`` go on being true when it says everything it routes
needs an administrator.

Read-only by design - see ``customer_views``.
"""
from django.urls import path

from .customer_views import CustomerPlanListAPIView, CustomerSubscriptionAPIView


app_name = 'kraios_billing'

urlpatterns = [
    path('plans/', CustomerPlanListAPIView.as_view(), name='plans'),
    path('subscription/', CustomerSubscriptionAPIView.as_view(), name='subscription'),
]
