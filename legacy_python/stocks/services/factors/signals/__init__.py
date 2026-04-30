"""
AlphaSignal 因子实现库。

本包导入时会自动递归扫描所有子模块，触发 @register 装饰器注册因子。

约定：
- 每个因子一个 .py 文件（或按逻辑放在子模块里）
- 文件内必须 `from stocks.services.factors.us_registry import AlphaSignal, register`
- 每个因子类必须 `@register`
"""

import importlib
import logging
import pkgutil

from services.config import LOG_LEVEL

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


def _autoload() -> None:
    """递归导入本包所有子模块，触发 @register。"""
    n_loaded = 0
    for finder, name, is_pkg in pkgutil.walk_packages(__path__, prefix=f"{__name__}."):
        if name.endswith(".__init__"):
            continue
        try:
            importlib.import_module(name)
            n_loaded += 1
        except Exception as e:
            logger.error(f"Failed to autoload factor module {name}: {e}", exc_info=True)
    logger.debug(f"factors.signals autoload: {n_loaded} modules imported")


_autoload()
