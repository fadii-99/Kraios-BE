"""
Routes reachable WITHOUT any session, serving the admin console's queues.

Mounted at ``/api/v1/support/`` rather than under ``/api/v1/admin/``: the path
a visitor's browser calls should not say "admin", and the prefix that does is
the one every authenticated console endpoint lives behind. Keeping them apart
is what lets ``admin/urls.py`` go on being true when it says everything it
routes needs an administrator.
"""
from django.urls import path

from .public_views import ContactRequestAPIView


app_name = 'kraios_public'

urlpatterns = [
    path('contact/', ContactRequestAPIView.as_view(), name='contact'),
]
