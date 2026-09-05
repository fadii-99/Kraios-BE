"""Routes for the BIM engine, all under /api/v1/bim/.

One `include()` in config/urls.py mounts every one of these. Removing that line
removes the whole HTTP surface of this app.
"""
from django.urls import path

from bim.views import (
    BimExtractionDetailAPIView,
    BimExtractionListCreateAPIView,
    BimSourceDetailAPIView,
    BimSourceFileAPIView,
    BimSourceListCreateAPIView,
)

app_name = 'bim'

urlpatterns = [
    path('sources/', BimSourceListCreateAPIView.as_view(), name='source-list-create'),
    path('sources/<uuid:source_id>/', BimSourceDetailAPIView.as_view(), name='source-detail'),
    path('sources/<uuid:source_id>/file/', BimSourceFileAPIView.as_view(), name='source-file'),
    path(
        'sources/<uuid:source_id>/extractions/',
        BimExtractionListCreateAPIView.as_view(),
        name='extraction-list-create',
    ),
    path(
        'extractions/<uuid:extraction_id>/',
        BimExtractionDetailAPIView.as_view(),
        name='extraction-detail',
    ),
]
