"""ASGI config with Django Channels WebSocket routing."""
import os
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 上，使 'backend' 包可导入
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.core.settings')

django_asgi_app = get_asgi_application()

from backend.tasks.routing import websocket_urlpatterns

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': URLRouter(websocket_urlpatterns),
})
