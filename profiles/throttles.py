from rest_framework.throttling import UserRateThrottle


class ProfileOTPThrottle(UserRateThrottle):
    scope = 'profile_otp'


class PasswordResetThrottle(UserRateThrottle):
    scope = 'password_reset'
