from django.urls import path

from .views import (
    CsrfAPIView,
    CurrentUserAPIView,
    LoginAPIView,
    LogoutAPIView,
    RefreshAPIView,
    SignupRequestAPIView,
)


app_name = 'accounts'

urlpatterns = [
    path('signup-request/', SignupRequestAPIView.as_view(), name='signup-request'),
    path('csrf/', CsrfAPIView.as_view(), name='csrf'),
    path('login/', LoginAPIView.as_view(), name='login'),
    path('refresh/', RefreshAPIView.as_view(), name='refresh'),
    path('logout/', LogoutAPIView.as_view(), name='logout'),
    path('me/', CurrentUserAPIView.as_view(), name='me'),
]
