"""Read-oriented admin for support.

Extractions are not editable here. The plan and the quality report are outputs
of a pipeline; hand-editing one in the admin would produce a row that no longer
corresponds to any run, which is exactly the state that makes a support
question unanswerable.
"""
from django.contrib import admin

from bim.models import BimExtraction, BimSource


@admin.register(BimSource)
class BimSourceAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'source_kind', 'image_width', 'image_height', 'created_at')
    list_filter = ('source_kind', 'created_at')
    search_fields = ('name', 'original_name', 'owner__email')
    readonly_fields = ('id', 'created_at', 'updated_at')
    list_select_related = ('owner',)


@admin.register(BimExtraction)
class BimExtractionAdmin(admin.ModelAdmin):
    list_display = ('id', 'source', 'status', 'grade', 'score', 'progress', 'created_at')
    list_filter = ('status', 'grade', 'created_at')
    search_fields = ('id', 'source__name', 'source__owner__email')
    list_select_related = ('source',)
    readonly_fields = tuple(
        field.name for field in BimExtraction._meta.fields
    )

    def has_add_permission(self, request):
        return False
