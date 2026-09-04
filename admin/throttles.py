"""
Rate limits for the admin console.

The sign-in limit is keyed on ``(email, client IP)`` rather than on either
alone. Keying on the email would let anyone lock a real administrator out of
their own console by spraying their address; keying on the IP alone would let
one host walk a list of addresses at one attempt each. The pair costs an
attacker a fresh IP for every address they want to try.

Both throttles fail OPEN when the cache backend is unavailable. A Redis outage
should degrade rate limiting, not take the console offline — the database-backed
lockout in ``services_auth`` still applies, and the failure is logged.
"""
import logging

from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle, UserRateThrottle


logger = logging.getLogger(__name__)


class _FailOpenMixin:
    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except Exception:
            logger.exception(
                'Throttle backend unavailable for scope %s; allowing the request.',
                getattr(self, 'scope', 'unknown'),
            )
            return True


class AdminLoginThrottle(_FailOpenMixin, SimpleRateThrottle):
    scope = 'admin_login'

    def get_cache_key(self, request, view):
        email = ''
        if isinstance(getattr(request, 'data', None), dict):
            email = str(request.data.get('email') or '').strip().lower()[:254]

        return self.cache_format % {
            'scope': self.scope,
            'ident': f'{email}|{self.get_ident(request)}',
        }


class AdminApiThrottle(_FailOpenMixin, UserRateThrottle):
    """A ceiling on all admin traffic from one signed-in administrator.

    It is not a security control on its own - the console is already behind
    authentication - but it stops a looping page or a runaway script from
    turning one session into a load problem for everyone else.
    """

    scope = 'admin_api'


class PublicContactThrottle(_FailOpenMixin, AnonRateThrottle):
    """
    The public contact form, keyed on the client IP.

    A WRITE reached by anyone, so the limit is a spam ceiling rather than a
    fairness one: a person with a genuine enquiry sends one message, maybe two
    if they thought of something else. It fails open with everything else here,
    because a cache outage that silences the contact form is a worse failure
    than an unthrottled one - the queue is read by a human who can delete.
    """

    scope = 'public_contact'
