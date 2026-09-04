"""
Route table for the admin console API.

Mounted at ``/api/v1/admin/`` rather than ``/admin/`` because Django's own
admin site already owns that prefix, and rather than sharing the customer
prefix because these endpoints answer to a different credential entirely.

The four endpoints under ``auth/`` are the only ones reachable without a
session. Everything else inherits ``AdminAPIView``; a route added here without
one of those two base classes would not compile into a working view, which is
the point of having exactly two.
"""
from django.urls import path

from . import views


app_name = 'kraios_admin'

urlpatterns = [
    # Session
    path('auth/csrf/', views.AdminCsrfAPIView.as_view(), name='csrf'),
    path('auth/login/', views.AdminLoginAPIView.as_view(), name='login'),
    path('auth/refresh/', views.AdminRefreshAPIView.as_view(), name='refresh'),
    path('auth/logout/', views.AdminLogoutAPIView.as_view(), name='logout'),
    path('auth/me/', views.AdminMeAPIView.as_view(), name='me'),
    path(
        'auth/change-password/',
        views.AdminChangePasswordAPIView.as_view(),
        name='change-password',
    ),

    # Overview
    path('dashboard/', views.DashboardOverviewAPIView.as_view(), name='dashboard'),

    # Users
    path('users/', views.UserListAPIView.as_view(), name='user-list'),
    path('users/<int:user_id>/', views.UserDetailAPIView.as_view(), name='user-detail'),
    path(
        'users/<int:user_id>/status/',
        views.UserStatusAPIView.as_view(),
        name='user-status',
    ),
    path(
        'users/<int:user_id>/generate-password/',
        views.UserGeneratePasswordAPIView.as_view(),
        name='user-generate-password',
    ),
    path(
        'users/<int:user_id>/subscription/',
        views.UserSubscriptionAPIView.as_view(),
        name='user-subscription',
    ),

    # Meetings
    path('meetings/', views.MeetingListAPIView.as_view(), name='meeting-list'),
    path('meetings/slots/', views.MeetingSlotsAPIView.as_view(), name='meeting-slots'),
    path(
        'meetings/<uuid:meeting_id>/',
        views.MeetingDetailAPIView.as_view(),
        name='meeting-detail',
    ),
    path(
        'meetings/<uuid:meeting_id>/status/',
        views.MeetingStatusAPIView.as_view(),
        name='meeting-status',
    ),
    path(
        'meetings/<uuid:meeting_id>/reschedule/',
        views.MeetingRescheduleAPIView.as_view(),
        name='meeting-reschedule',
    ),

    # Availability
    path('availability/', views.AvailabilityAPIView.as_view(), name='availability'),
    path(
        'availability/blackouts/',
        views.AvailabilityBlackoutAPIView.as_view(),
        name='availability-blackouts',
    ),
    path(
        'availability/blackouts/<int:blackout_id>/',
        views.AvailabilityBlackoutDetailAPIView.as_view(),
        name='availability-blackout-detail',
    ),

    # Subscription plans
    path('plans/', views.PlanListAPIView.as_view(), name='plan-list'),
    path('plans/<str:plan_id>/', views.PlanDetailAPIView.as_view(), name='plan-detail'),
    path(
        'plans/<str:plan_id>/status/',
        views.PlanStatusAPIView.as_view(),
        name='plan-status',
    ),

    # Usage
    path('usage/', views.UsageListAPIView.as_view(), name='usage-list'),
    path('usage/firms/', views.UsageFirmsAPIView.as_view(), name='usage-firms'),

    # Support
    path('support/', views.SupportListAPIView.as_view(), name='support-list'),
    path(
        'support/<str:request_id>/',
        views.SupportDetailAPIView.as_view(),
        name='support-detail',
    ),

    # Audit
    path('audit-logs/', views.AuditLogAPIView.as_view(), name='audit-logs'),
]
