from django.urls import path

from .views import (
    BookingDaysAPIView,
    BookingSlotsAPIView,
    CsrfAPIView,
    CurrentUserAPIView,
    ForgotPasswordConfirmAPIView,
    ForgotPasswordRequestAPIView,
    LoginAPIView,
    LogoutAPIView,
    RefreshAPIView,
    SignupRequestAPIView,
)


app_name = 'accounts'

urlpatterns = [
    path('signup-request/', SignupRequestAPIView.as_view(), name='signup-request'),

    # What the signup form's calendar and slot list are drawn from - the
    # administrator's availability, read without a session.
    path('booking/days/', BookingDaysAPIView.as_view(), name='booking-days'),
    path('booking/slots/', BookingSlotsAPIView.as_view(), name='booking-slots'),
    path(
        'forgot-password/request/',
        ForgotPasswordRequestAPIView.as_view(),
        name='forgot-password-request',
    ),
    path(
        'forgot-password/confirm/',
        ForgotPasswordConfirmAPIView.as_view(),
        name='forgot-password-confirm',
    ),
    path('csrf/', CsrfAPIView.as_view(), name='csrf'),
    path('login/', LoginAPIView.as_view(), name='login'),
    path('refresh/', RefreshAPIView.as_view(), name='refresh'),
    path('logout/', LogoutAPIView.as_view(), name='logout'),
    path('me/', CurrentUserAPIView.as_view(), name='me'),
]
