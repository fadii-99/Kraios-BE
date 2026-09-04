"""
Deploy-time checks for the admin console.

These are Django system checks rather than documentation because a note in a
README does not fail a deploy. Each one guards a configuration that is silently
wrong rather than loudly broken: the console would start, appear to work, and
be either insecure or unable to deliver the one email the onboarding flow
depends on.

``check_admin_cookie_policy`` runs on every ``manage.py check`` because the
configuration it rejects is wrong everywhere. The rest are registered with
``deploy=True`` and therefore only run under ``manage.py check --deploy``: they
compare against the DEPLOYED posture, and Django's test runner forces
``DEBUG = False``, so an ordinary check would fire them during every test run
and teach everyone to ignore them.
"""
from django.conf import settings
from django.core.checks import Error, Tags, Warning, register


CONSOLE_EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'


@register(Tags.security)
def check_admin_cookie_policy(app_configs, **kwargs):
    """A cross-site admin cookie without Secure is a cookie that never arrives."""
    problems = []

    samesite = getattr(settings, 'ADMIN_AUTH_COOKIE_SAMESITE', 'Lax')
    secure = getattr(settings, 'ADMIN_AUTH_COOKIE_SECURE', False)

    if str(samesite).lower() == 'none' and not secure:
        problems.append(
            Error(
                'ADMIN_AUTH_COOKIE_SAMESITE is "None" but the cookie is not Secure.',
                hint=(
                    'Browsers reject SameSite=None without Secure, so the admin '
                    'session cookie would be dropped on every request. Serve the '
                    'API over HTTPS, or put the console on the same site as the '
                    'API and use "Lax".'
                ),
                id='kraios_admin.E001',
            )
        )

    return problems


@register(Tags.security, deploy=True)
def check_admin_cookie_is_secure_when_deployed(app_configs, **kwargs):
    if getattr(settings, 'ADMIN_AUTH_COOKIE_SECURE', False):
        return []

    return [
        Error(
            'ADMIN_AUTH_COOKIE_SECURE is False.',
            hint='The admin session would travel in clear text.',
            id='kraios_admin.E002',
        )
    ]


@register(Tags.security, deploy=True)
def check_admin_email_delivery(app_configs, **kwargs):
    """
    Credentials reach a customer by email and by no other route.

    With the console backend configured in a deployed environment, an
    administrator would press "Generate Password", see a success, and the
    customer would never receive anything - and the password cannot be
    recovered afterwards.
    """
    if settings.EMAIL_BACKEND == CONSOLE_EMAIL_BACKEND:
        return [
            Error(
                'EMAIL_BACKEND is the console backend outside development.',
                hint=(
                    'Generated customer passwords and meeting reminders would be '
                    'printed to a log instead of being delivered. Set '
                    'DJANGO_EMAIL_BACKEND to the SMTP backend.'
                ),
                id='kraios_admin.E003',
            )
        ]

    return []


@register(Tags.security, deploy=True)
def check_admin_cors_configuration(app_configs, **kwargs):
    """Credentialed cross-origin requests need the console's origin listed."""
    if getattr(settings, 'CORS_ALLOW_CREDENTIALS', False) and not settings.CORS_ALLOWED_ORIGINS:
        return [
            Warning(
                'CORS_ALLOW_CREDENTIALS is on but no origins are allowed.',
                hint=(
                    'Add the admin console origin to DJANGO_CORS_ALLOWED_ORIGINS '
                    'and DJANGO_CSRF_TRUSTED_ORIGINS, or the console cannot sign in.'
                ),
                id='kraios_admin.W001',
            )
        ]

    return []
