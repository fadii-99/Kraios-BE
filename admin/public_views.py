"""
The one PUBLIC write into the admin console's data.

WHY THIS IS A SEPARATE MODULE. ``views.py`` states an invariant it is worth
keeping true: every view there inherits ``AdminAPIView`` or
``PublicAdminAPIView``, so a route added to ``admin/urls.py`` is either
authenticated or one of the four session endpoints, and there is no third
option. The contact form is a genuine third thing - anonymous, unauthenticated,
and not part of establishing an admin session - so it lives here and is routed
from ``public_urls.py``, which is mounted outside ``/api/v1/admin/``.

WHAT IT CAN AND CANNOT SET. The serializer is an allowlist of seven fields;
``id``, ``submittedAt``, ``status``, ``priority`` and ``assignee`` are added by
``dummy_data.create_support_request`` and cannot be sent. A stranger deciding
their own priority is not a feature.

WHAT IT ANSWERS WITH. A reference and a message, never the stored row. The
queue is administrators' - the sender has no session, so there is nothing they
could be authorised to read back, and echoing the record would hand an
enumerator a way to confirm what landed.
"""
import logging

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import dummy_data
from .serializers import ContactRequestSerializer
from .throttles import PublicContactThrottle


logger = logging.getLogger(__name__)


class ContactRequestAPIView(APIView):
    """``POST`` one contact-form submission into the support queue."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicContactThrottle]

    def post(self, request):
        serializer = ContactRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        row = dummy_data.create_support_request(serializer.validated_data)

        # The reference, the topic and the derived priority - enough to trace
        # one message from the log to the queue without putting the sender's
        # words in it.
        logger.info(
            'Contact request %s filed (%s, %s priority).',
            row['id'],
            row['topic'],
            row['priority'],
        )

        return Response(
            {
                'message': 'Thanks — your message has reached the KRAIOS team.',
                'request_id': row['id'],
            },
            status=status.HTTP_201_CREATED,
        )
