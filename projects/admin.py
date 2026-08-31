from django.contrib import admin

from .models import (
    BOQVersion,
    Conversation,
    ConversationMessage,
    FloorPlanVersion,
    ProcessingJob,
    Project,
    ProjectAsset,
    ProjectDocument,
    ThreeDVersion,
)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'workflow', 'created_at', 'updated_at')
    list_filter = ('workflow',)
    search_fields = ('name', 'owner__email')
    readonly_fields = ('workflow', 'created_at', 'updated_at')


@admin.register(ProjectAsset)
class ProjectAssetAdmin(admin.ModelAdmin):
    list_display = ('original_name', 'project', 'kind', 'size', 'created_at')
    list_filter = ('kind',)
    search_fields = ('original_name', 'project__name', 'project__owner__email')
    readonly_fields = ('created_at',)


@admin.register(ProcessingJob)
class ProcessingJobAdmin(admin.ModelAdmin):
    list_display = ('id', 'project', 'job_type', 'status', 'progress', 'created_at')
    list_filter = ('job_type', 'status')
    search_fields = ('project__name', 'project__owner__email', 'celery_task_id')
    readonly_fields = (
        'project',
        'created_by',
        'job_type',
        'status',
        'progress',
        'message',
        'error',
        'parameters',
        'celery_task_id',
        'output_asset',
        'created_at',
        'started_at',
        'completed_at',
        'updated_at',
    )

    def has_add_permission(self, request):
        return False


admin.site.register(FloorPlanVersion)
admin.site.register(Conversation)
admin.site.register(ConversationMessage)
admin.site.register(ThreeDVersion)
admin.site.register(BOQVersion)
admin.site.register(ProjectDocument)
