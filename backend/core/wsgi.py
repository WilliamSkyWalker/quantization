"""WSGI config for backend."""
import os
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 上，使 'backend' 包可导入
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.core.settings')

application = get_wsgi_application()
