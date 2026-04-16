"""URL configuration for backend."""
import logging

from django.urls import path, include, re_path
from django.views.static import serve
from django.views.generic import TemplateView
from django.conf import settings

logger = logging.getLogger(__name__)


def _frontend_file(request, path=''):
    """Serve files from frontend/dist (vite.svg, favicon, etc.)."""
    return serve(request, path, document_root=settings.FRONTEND_DIST)


urlpatterns = [
    # API routes — 各 app 自管理，多 include 合并到 /api/ 前缀
    path('api/', include('stocks.urls')),
    path('api/', include('backtest.urls')),
    path('api/', include('trading.urls')),
    path('api/', include('sentiment.urls')),
    # Frontend static assets (/assets/*)
    re_path(r'^assets/(?P<path>.*)$', serve, {'document_root': settings.FRONTEND_DIST / 'assets'}),
    # Root-level static files (vite.svg, favicon.ico, etc.)
    re_path(r'^(?P<path>[\w.-]+\.\w+)$', _frontend_file),
    # SPA catch-all — all other routes serve index.html
    re_path(r'^(?!api/).*$', TemplateView.as_view(template_name='index.html'), name='spa'),
]
