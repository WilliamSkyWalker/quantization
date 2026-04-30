"""Custom DRF renderer that ensures UTF-8 charset in Content-Type header."""
import logging

from rest_framework.renderers import JSONRenderer

logger = logging.getLogger(__name__)


class UTF8JSONRenderer(JSONRenderer):
    """JSONRenderer with explicit charset=utf-8 to prevent garbled CJK text."""
    charset = 'utf-8'
