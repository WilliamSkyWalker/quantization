"""ASGI config with Django Channels WebSocket routing."""
import os
import sys
from pathlib import Path

# ---------- Python 3.14 compat ----------
# Python 3.14 raises RuntimeError("cannot schedule new futures after
# interpreter shutdown") when the event loop's default ThreadPoolExecutor
# is shut down (e.g. during daphne hot-reload). Patch run_in_executor to
# transparently replace the dead executor so Django/channels keep working.
import asyncio
from concurrent.futures import ThreadPoolExecutor

_orig_run_in_executor = asyncio.BaseEventLoop.run_in_executor


def _resilient_run_in_executor(self, executor, func, *args):
    try:
        return _orig_run_in_executor(self, executor, func, *args)
    except RuntimeError:
        try:
            new_executor = ThreadPoolExecutor()
            if executor is None:
                self._default_executor = new_executor
            return _orig_run_in_executor(self, new_executor, func, *args)
        except RuntimeError:
            # Interpreter truly shutting down — return no-op future
            fut = self.create_future()
            fut.set_result(None)
            return fut


asyncio.BaseEventLoop.run_in_executor = _resilient_run_in_executor
# -----------------------------------------

# 确保项目根目录在 sys.path 上
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

django_asgi_app = get_asgi_application()

from tasks.routing import websocket_urlpatterns

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': URLRouter(websocket_urlpatterns),
})
