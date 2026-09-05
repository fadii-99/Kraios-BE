import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Docker containers get their env from `env_file:`, so this is a no-op there.
# Locally (runserver/celery run outside Docker), this loads .env into the
# process so both read the same config without exporting vars by hand.
load_dotenv(BASE_DIR / '.env')


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/

DEBUG = os.environ.get('DJANGO_DEBUG', 'True').lower() == 'true'

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')

if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'django-insecure-local-development-only'
    else:
        raise RuntimeError('DJANGO_SECRET_KEY is required when DEBUG is False.')

allowed_hosts_value = os.environ.get(
    'DJANGO_ALLOWED_HOSTS',
    'localhost,127.0.0.1',
)
ALLOWED_HOSTS = [
    host.strip()
    for host in allowed_hosts_value.split(',')
    if host.strip()
]

csrf_origins_value = os.environ.get('DJANGO_CSRF_TRUSTED_ORIGINS', '')
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in csrf_origins_value.split(',')
    if origin.strip()
]

cors_origins_value = os.environ.get('DJANGO_CORS_ALLOWED_ORIGINS', '')
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in cors_origins_value.split(',')
    if origin.strip()
]
# Authentication uses HttpOnly cookies, so browsers must be explicitly allowed
# to include and receive cookies on approved cross-origin API requests.
CORS_ALLOW_CREDENTIALS = True
CORS_URLS_REGEX = r'^/api/.*$'

# Application definition

INSTALLED_APPS = [
    'daphne',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'corsheaders',
    'rest_framework_simplejwt.token_blacklist',
    'accounts',
    'profiles',
    'projects',
    # The admin console API. Its package is `admin`, but its app LABEL is
    # `kraios_admin` because `django.contrib.admin` already owns the label
    # `admin` and two apps may not share one. See admin/apps.py.
    'admin.apps.KraiosAdminConfig',
    # The BIM engine. Fully self-contained - see bim/README.md for how to
    # remove it: this line, one line in config/urls.py, and the directory.
    'bim',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

postgres_database = os.environ.get('POSTGRES_DB')

if postgres_database:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': postgres_database,
            'USER': os.environ.get('POSTGRES_USER', ''),
            'PASSWORD': os.environ.get('POSTGRES_PASSWORD', ''),
            'HOST': os.environ.get('POSTGRES_HOST', 'db'),
            'PORT': os.environ.get('POSTGRES_PORT', '5432'),
            'CONN_MAX_AGE': 60,
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

if DEBUG:
    staticfiles_backend = 'django.contrib.staticfiles.storage.StaticFilesStorage'
else:
    staticfiles_backend = (
        'whitenoise.storage.CompressedManifestStaticFilesStorage'
    )

STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': staticfiles_backend,
    },
}

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTH_USER_MODEL = 'accounts.User'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'accounts.authentication.CookieJWTAuthentication',
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_THROTTLE_RATES': {
        'profile_otp': '5/hour',
        'password_reset': '10/hour',
        # Keyed on (email, client IP) - see admin/throttles.py. Generous enough
        # for somebody mistyping a password, useless for guessing one.
        'admin_login': '30/hour',
        # A ceiling on all admin traffic from one signed-in administrator, so a
        # looping console page cannot become everyone else's load problem.
        'admin_api': '1200/hour',
        # The signup calendar, keyed on the client IP - see accounts/throttles.py.
        # Paging through months and picking a date costs a handful of requests;
        # this is only a ceiling on scraping the schedule.
        'public_booking': '240/hour',
        # The public contact form, keyed on the client IP - see
        # admin/throttles.py. A write anybody can reach, so this is a spam
        # ceiling: one genuine enquiry, and room to think of a second.
        'public_contact': '10/hour',
    },
}

AUTH_ACCESS_COOKIE_NAME = 'kraios_access' if DEBUG else '__Host-kraios_access'
AUTH_REFRESH_COOKIE_NAME = 'kraios_refresh' if DEBUG else '__Host-kraios_refresh'
AUTH_COOKIE_SECURE = not DEBUG
AUTH_COOKIE_SAMESITE = 'Lax'
AUTH_ACCESS_COOKIE_AGE = 5 * 60
AUTH_REFRESH_COOKIE_AGE = 7 * 24 * 60 * 60

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=5),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'CHECK_REVOKE_TOKEN': True,
}

# ---------------------------------------------------------------------------
# KRAIOS admin console
# ---------------------------------------------------------------------------
#
# The console is a separate browser application with a separate credential. Its
# cookies are named differently from the customer ones on purpose: a customer
# session is never presented to an admin endpoint, so it cannot be replayed
# there even when both applications share a hostname. The tokens additionally
# carry an admin scope claim, which the customer login endpoint cannot mint.

ADMIN_AUTH_ACCESS_COOKIE_NAME = (
    'kraios_admin_access' if DEBUG else '__Host-kraios_admin_access'
)
ADMIN_AUTH_REFRESH_COOKIE_NAME = (
    'kraios_admin_refresh' if DEBUG else '__Host-kraios_admin_refresh'
)
ADMIN_AUTH_COOKIE_SECURE = not DEBUG

# 'Lax' is correct while the console and the API share a registrable domain
# (localhost:5174 -> localhost:8000 in development, admin.example.com ->
# api.example.com in production). Set this to 'None' ONLY if they are on
# genuinely different sites, and only together with HTTPS - a system check
# refuses that combination without secure cookies.
ADMIN_AUTH_COOKIE_SAMESITE = os.environ.get(
    'DJANGO_ADMIN_COOKIE_SAMESITE',
    AUTH_COOKIE_SAMESITE,
)

# Deliberately shorter than the customer session: this credential can
# deactivate accounts and issue passwords.
ADMIN_ACCESS_TOKEN_LIFETIME = timedelta(
    minutes=int(os.environ.get('ADMIN_ACCESS_TOKEN_MINUTES', '15'))
)
ADMIN_REFRESH_TOKEN_LIFETIME = timedelta(
    hours=int(os.environ.get('ADMIN_REFRESH_TOKEN_HOURS', '8'))
)

# Where the placeholder store for plans, per-user subscriptions and support
# lives. NOT a database - see admin/dummy_data.py. Deleting this file resets
# that data and touches nothing else.
KRAIOS_ADMIN_DUMMY_STORE_PATH = Path(
    os.environ.get(
        'KRAIOS_ADMIN_DUMMY_STORE_PATH',
        BASE_DIR / 'ai_state' / 'admin_placeholder_store.json',
    )
)

# Meeting reminders. The lead time is what the customer is promised; the grace
# is how far past it the task will still deliver, so a worker that was down for
# a few minutes does not silently drop a reminder.
KRAIOS_ADMIN_REMINDER_LEAD_MINUTES = int(
    os.environ.get('KRAIOS_ADMIN_REMINDER_LEAD_MINUTES', '60')
)
KRAIOS_ADMIN_REMINDER_GRACE_MINUTES = int(
    os.environ.get('KRAIOS_ADMIN_REMINDER_GRACE_MINUTES', '10')
)

# Who is alerted about an upcoming call. Empty means "every active
# administrator", which is the right default for a small team.
KRAIOS_ADMIN_ALERT_EMAILS = [
    address.strip()
    for address in os.environ.get('KRAIOS_ADMIN_ALERT_EMAILS', '').split(',')
    if address.strip()
]

# Printed in the credentials email an administrator issues, so it has to point
# at the CUSTOMER application, not the console.
KRAIOS_APP_SIGN_IN_URL = os.environ.get(
    'KRAIOS_APP_SIGN_IN_URL',
    'http://localhost:5173/login',
)

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = (
    os.environ.get('DJANGO_SECURE_SSL_REDIRECT', 'False').lower() == 'true'
)
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

DEFAULT_FROM_EMAIL = os.environ.get('DJANGO_DEFAULT_FROM_EMAIL', 'noreply@kraios.local')
EMAIL_BACKEND = os.environ.get(
    'DJANGO_EMAIL_BACKEND',
    'django.core.mail.backends.console.EmailBackend',
)
EMAIL_HOST = os.environ.get('DJANGO_EMAIL_HOST', '')
EMAIL_PORT = int(os.environ.get('DJANGO_EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('DJANGO_EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('DJANGO_EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = os.environ.get('DJANGO_EMAIL_USE_TLS', 'True').lower() == 'true'
EMAIL_USE_SSL = os.environ.get('DJANGO_EMAIL_USE_SSL', 'False').lower() == 'true'
EMAIL_TIMEOUT = int(os.environ.get('DJANGO_EMAIL_TIMEOUT', '15'))
KRAIOS_SUPPORT_EMAIL = os.environ.get(
    'KRAIOS_SUPPORT_EMAIL',
    EMAIL_HOST_USER or DEFAULT_FROM_EMAIL,
)

if EMAIL_USE_TLS and EMAIL_USE_SSL:
    raise RuntimeError(
        'Use either DJANGO_EMAIL_USE_TLS or DJANGO_EMAIL_USE_SSL, not both.'
    )

PROJECT_UPLOAD_MAX_BYTES = int(
    os.environ.get('PROJECT_UPLOAD_MAX_BYTES', 25 * 1024 * 1024)
)
AI_PLACEHOLDER_DELAY_SECONDS = int(
    os.environ.get('AI_PLACEHOLDER_DELAY_SECONDS', '5')
)
AI_PIPELINE_ENABLED = (
    os.environ.get('AI_PIPELINE_ENABLED', 'False').lower() == 'true'
)

REDIS_URL = os.environ.get('REDIS_URL', 'redis://redis:6379/0')

if DEBUG:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'kraios-development-cache',
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': REDIS_URL.replace('/0', '/1'),
        }
    }

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = int(os.environ.get('CELERY_TASK_TIME_LIMIT', '1200'))
CELERY_TASK_SOFT_TIME_LIMIT = int(
    os.environ.get('CELERY_TASK_SOFT_TIME_LIMIT', '1140')
)
CELERY_TASK_ALWAYS_EAGER = (
    os.environ.get('CELERY_TASK_ALWAYS_EAGER', 'False').lower() == 'true'
)

# Periodic work. Run with `celery -A config beat`; compose.yaml has a service
# for it. The reminder interval has to be well under
# KRAIOS_ADMIN_REMINDER_GRACE_MINUTES or a reminder can fall between two runs.
CELERY_BEAT_SCHEDULE = {
    'kraios-admin-meeting-reminders': {
        'task': 'admin.tasks.send_due_meeting_reminders',
        'schedule': 300.0,
    },
    'kraios-admin-prune-login-attempts': {
        'task': 'admin.tasks.prune_login_attempts',
        'schedule': 3600.0,
    },
}

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {'hosts': [REDIS_URL]},
    },
}
