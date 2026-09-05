"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def health_check(request):
    return JsonResponse({'status': 'ok'})

urlpatterns = [
    path('health/', health_check, name='health'),
    path('admin/', admin.site.urls),
    path('api/v1/auth/', include('accounts.urls')),
    path('api/v1/profile/', include('profiles.urls')),
    path('api/v1/projects/', include('projects.urls')),
    # The admin console API. Mounted under /api/v1/admin/ because Django's own
    # admin site already owns the bare /admin/ prefix above; the string form of
    # include() is used so the local name `admin` (django.contrib.admin) is not
    # shadowed by the app package of the same name.
    path('api/v1/admin/', include('admin.urls')),
    # The public contact form writes into the console's support queue. Kept
    # off the /admin/ prefix on purpose - see admin/public_urls.py.
    path('api/v1/support/', include('admin.public_urls')),
    # What a signed-in CUSTOMER may read of the plan catalogue and of their own
    # subscription - the console's records, a different reader, a different
    # field set. See admin/customer_urls.py.
    path('api/v1/billing/', include('admin.customer_urls')),
    # The BIM engine. Self-contained: this line, one entry in INSTALLED_APPS,
    # and the `bim` package are the whole feature. See bim/README.md.
    path('api/v1/bim/', include('bim.urls')),
]
