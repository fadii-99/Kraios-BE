"""HTTP surface for the BIM engine.

Every view resolves its object through `_owned_source`, which filters on
`owner=request.user`. There is no code path in this app that reaches a
`BimSource` or a `BimExtraction` any other way, so a caller cannot read or act
on somebody else's drawing by guessing a UUID — the lookup simply does not find
it and answers 404.

Views here parse, authorize, call one service function, and shape a response.
The decisions live in `services.py`.
"""
from __future__ import annotations

import logging

from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from bim import services
from bim.models import BimExtraction, BimSource
from bim.serializers import (
    BimExtractionSerializer,
    BimExtractionSummarySerializer,
    BimSourceSerializer,
    BimSourceUploadSerializer,
)

logger = logging.getLogger(__name__)

# A page of sources. The UI shows a gallery, not an archive; a user who has
# uploaded more than this is served by search, which this app does not have yet.
SOURCE_PAGE_SIZE = 60


def _owned_source(request, source_id) -> BimSource:
    return get_object_or_404(BimSource, id=source_id, owner=request.user)


class BimSourceListCreateAPIView(APIView):
    """GET the caller's floor plans; POST a new one."""

    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        sources = (
            BimSource.objects.filter(owner=request.user)
            # The serializer reads one extraction per source. Without this
            # prefetch that is a query per row; with it, two queries total.
            .prefetch_related('extractions')
            .order_by('-created_at')[:SOURCE_PAGE_SIZE]
        )
        return Response(
            BimSourceSerializer(sources, many=True, context={'request': request}).data
        )

    def post(self, request):
        payload = BimSourceUploadSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        source = services.create_source(
            owner=request.user,
            uploaded_file=payload.validated_data['file'],
            name=payload.validated_data.get('name', ''),
        )
        return Response(
            BimSourceSerializer(source, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )


class BimSourceDetailAPIView(APIView):
    def get(self, request, source_id):
        source = _owned_source(request, source_id)
        return Response(BimSourceSerializer(source, context={'request': request}).data)

    def delete(self, request, source_id):
        source = _owned_source(request, source_id)
        stored_file = source.file
        source.delete()
        # After the row is gone, so a storage backend that is slow or briefly
        # unavailable cannot leave the user looking at a plan they deleted.
        # An orphaned file is recoverable; a phantom row is confusing.
        try:
            stored_file.delete(save=False)
        except OSError:
            logger.warning('bim.source %s deleted but its file remained', source_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class BimSourceFileAPIView(APIView):
    """Serve the uploaded drawing to its owner.

    Media is not publicly served in production, and a floor plan is the user's
    own document, so it is streamed through an authenticated view rather than
    linked directly out of storage.
    """

    def get(self, request, source_id):
        source = _owned_source(request, source_id)
        try:
            handle = source.file.open('rb')
        except (OSError, ValueError):
            logger.exception('bim.source %s file is missing from storage', source_id)
            return Response(
                {'detail': 'The stored file could not be read.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return FileResponse(
            handle,
            content_type=source.content_type or 'application/octet-stream',
            filename=source.original_name,
        )


class BimExtractionListCreateAPIView(APIView):
    """GET this source's extraction history; POST to start a new one."""

    def get(self, request, source_id):
        source = _owned_source(request, source_id)
        extractions = source.extractions.order_by('-created_at')
        return Response(
            BimExtractionSummarySerializer(
                extractions, many=True, context={'request': request}
            ).data
        )

    def post(self, request, source_id):
        source = _owned_source(request, source_id)
        extraction = services.start_extraction(source=source, user=request.user)
        return Response(
            BimExtractionSummarySerializer(extraction, context={'request': request}).data,
            status=status.HTTP_202_ACCEPTED,
        )


class BimExtractionDetailAPIView(APIView):
    """The full result, and the endpoint the client polls while it runs."""

    def get(self, request, extraction_id):
        extraction = get_object_or_404(
            BimExtraction.objects.select_related('source'),
            id=extraction_id,
            source__owner=request.user,
        )
        return Response(
            BimExtractionSerializer(extraction, context={'request': request}).data
        )
