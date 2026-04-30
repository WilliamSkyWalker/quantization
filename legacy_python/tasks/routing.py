"""WebSocket URL routing."""
import logging

from django.urls import re_path

logger = logging.getLogger(__name__)

from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/tasks/$', consumers.TaskConsumer.as_asgi()),
    re_path(r'ws/polymarket/$', consumers.PolymarketConsumer.as_asgi()),
]
