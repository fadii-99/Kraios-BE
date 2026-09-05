"""Wire format for the BIM engine.

`BimExtraction.error` is never serialised. It carries provider messages, file
paths and exception text, all of which are useful in a log and none of which
belong in a response. Clients get a fixed sentence and the extraction id; the
detail is looked up by that id in the logs or the admin.

Two extraction serializers exist because the plan JSON is large — a commercial
floor can run to a few hundred kilobytes — and a list of ten extractions that
each carried its full plan would be a slow response nobody reads. The list gets
the summary; the detail gets everything.
"""
from __future__ import annotations

from django.urls import reverse
from rest_framework import serializers

from bim.models import BimExtraction, BimSource

# What a client is told when an extraction failed. Deliberately identical for
# every cause: a message that varies with the internal error is a message that
# leaks the internal error.
FAILURE_MESSAGE = (
    'The floor plan could not be converted. Try a clearer or higher-resolution '
    'drawing, or run it again.'
)


class BimSourceSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()
    latest_extraction = serializers.SerializerMethodField()

    class Meta:
        model = BimSource
        fields = [
            'id',
            'name',
            'original_name',
            'content_type',
            'size',
            'image_width',
            'image_height',
            'source_kind',
            'file_url',
            'latest_extraction',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

    def get_file_url(self, source):
        # The app's own authenticated download route, not `file.url`: MEDIA is
        # not publicly served in production, and a plan is the user's drawing.
        # Resolved through `reverse` rather than written out, so remounting the
        # app under a different prefix cannot leave this pointing at nothing.
        path = reverse('bim:source-file', args=[source.id])
        request = self.context.get('request')
        return request.build_absolute_uri(path) if request else path

    def get_latest_extraction(self, source):
        # Prefetched by the list view; `.all()[:1]` rather than `.first()` so a
        # prefetched cache is reused instead of triggering a query per row.
        extractions = list(source.extractions.all()[:1])
        if not extractions:
            return None
        return BimExtractionSummarySerializer(extractions[0], context=self.context).data


class BimSourceUploadSerializer(serializers.Serializer):
    """Input only. The model instance is built by `services.create_source`."""

    file = serializers.FileField()
    name = serializers.CharField(max_length=150, required=False, allow_blank=True)


class BimExtractionSummarySerializer(serializers.ModelSerializer):
    """Enough to render a row and drive a progress poll. No plan, no issues."""

    error = serializers.SerializerMethodField()

    class Meta:
        model = BimExtraction
        fields = [
            'id',
            'source',
            'status',
            'progress',
            'message',
            'error',
            'score',
            'grade',
            'created_at',
            'started_at',
            'completed_at',
        ]
        read_only_fields = fields

    def get_error(self, extraction):
        return FAILURE_MESSAGE if extraction.status == BimExtraction.FAILED else ''


class BimExtractionSerializer(BimExtractionSummarySerializer):
    """The full result: the plan, the quality report and the attempt record."""

    class Meta(BimExtractionSummarySerializer.Meta):
        fields = BimExtractionSummarySerializer.Meta.fields + [
            'plan',
            'quality',
            'record',
        ]
        read_only_fields = fields
