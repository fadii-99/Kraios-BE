from django.urls import path

from .views import (
    DeleteAccountConfirmAPIView,
    DeleteAccountRequestAPIView,
    PasswordChangeConfirmAPIView,
    PasswordChangeRequestAPIView,
    ProfileAPIView,
)


app_name = 'profiles'

urlpatterns = [
    path('', ProfileAPIView.as_view(), name='detail'),
    path(
        'password-change/request/',
        PasswordChangeRequestAPIView.as_view(),
        name='password-change-request',
    ),
    path(
        'password-change/confirm/',
        PasswordChangeConfirmAPIView.as_view(),
        name='password-change-confirm',
    ),
    path(
        'delete-account/request/',
        DeleteAccountRequestAPIView.as_view(),
        name='delete-account-request',
    ),
    path(
        'delete-account/confirm/',
        DeleteAccountConfirmAPIView.as_view(),
        name='delete-account-confirm',
    ),
]

