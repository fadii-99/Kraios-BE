"""
Create or promote a KRAIOS administrator.

This is the ONLY way an administrator comes into existence. There is no signup
endpoint for one, no seeded credential in source, and no environment default -
which is the point: an administrator account that ships with the code is a
public account.

    python manage.py create_kraios_admin --email ops@example.com --role SUPER_ADMIN

The password is read from a prompt (never echoed) unless
``KRAIOS_ADMIN_BOOTSTRAP_PASSWORD`` is set in the environment, which is there
for a scripted first deploy. It is validated against the project's password
validators either way.
"""
import getpass
import os

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import User

from admin.models import AdminProfile


BOOTSTRAP_PASSWORD_ENV = 'KRAIOS_ADMIN_BOOTSTRAP_PASSWORD'


class Command(BaseCommand):
    help = 'Create a KRAIOS admin console account, or promote an existing user.'

    def add_arguments(self, parser):
        parser.add_argument('--email', required=True)
        parser.add_argument('--name', default='', help='Full name for a new account.')
        parser.add_argument(
            '--role',
            default=AdminProfile.ADMIN,
            choices=[choice[0] for choice in AdminProfile.ROLE_CHOICES],
        )
        parser.add_argument(
            '--reset-password',
            action='store_true',
            help='Set a new password on an existing account.',
        )

    def handle(self, *args, **options):
        email = options['email'].strip().lower()
        role = options['role']

        user = User.objects.filter(email__iexact=email).first()
        creating = user is None

        password = None
        if creating or options['reset_password']:
            password = self._read_password()

        with transaction.atomic():
            if creating:
                user = User.objects.create_user(
                    email=email,
                    password=password,
                    full_name=options['name'] or email.split('@')[0],
                    firm_name='KRAIOS',
                    country='',
                    is_active=True,
                )
            else:
                if password is not None:
                    user.set_password(password)
                if not user.is_active:
                    user.is_active = True
                user.save(update_fields=['password', 'is_active'])

            profile, profile_created = AdminProfile.objects.get_or_create(
                user=user,
                defaults={'role': role},
            )

            if not profile_created:
                profile.role = role
                profile.is_active = True
                # Any session opened under the previous role or password dies
                # here, so a promotion or demotion takes effect immediately.
                profile.session_secret = AdminProfile._meta.get_field(
                    'session_secret'
                ).get_default()
                profile.save(update_fields=['role', 'is_active', 'session_secret', 'updated_at'])

        action = 'Created' if creating else 'Updated'
        self.stdout.write(
            self.style.SUCCESS(f'{action} admin {user.email} with role {profile.role}.')
        )

    def _read_password(self):
        password = os.environ.get(BOOTSTRAP_PASSWORD_ENV)
        source = BOOTSTRAP_PASSWORD_ENV

        if not password:
            password = getpass.getpass('Admin password: ')
            confirmation = getpass.getpass('Confirm password: ')
            if password != confirmation:
                raise CommandError('The two passwords do not match.')
            source = 'prompt'

        if not password:
            raise CommandError('A password is required.')

        try:
            validate_password(password)
        except ValidationError as invalid:
            raise CommandError(
                'That password was rejected:\n  - ' + '\n  - '.join(invalid.messages)
            )

        self.stdout.write(f'Password read from {source}.')
        return password
