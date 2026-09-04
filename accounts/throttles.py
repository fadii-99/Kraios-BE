"""
Rate limits for the endpoints a visitor can reach without a session.

Keyed on the client IP, because that is the only identity a public request
has. The limit is a ceiling on scraping the booking calendar, not a UX
constraint: a visitor stepping through three months and picking a date costs a
handful of requests, so the rate is set well above anything a person does by
hand.

FAILS OPEN when the cache backend is unavailable, for the same reason
``admin/throttles.py`` does - a Redis outage should cost rate limiting, not
sign-ups.
"""
import logging

from rest_framework.throttling import AnonRateThrottle


logger = logging.getLogger(__name__)


class PublicBookingThrottle(AnonRateThrottle):
    scope = 'public_booking'

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except Exception:
            logger.exception(
                'Throttle backend unavailable for scope %s; allowing the request.',
                self.scope,
            )
            return True
