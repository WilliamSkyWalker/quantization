"""
向下兼容 Stub — DatabaseManager 已废弃。

A 股链路全面迁移到 Django ORM 后，此文件只保留一个 no-op DatabaseManager
让 US 旧代码（迁移中）能 import 通过。所有方法返回空 DataFrame，不会做实际查询。

待 US 链路完成 ORM 迁移后，整个 services/data/ 目录可删除。
"""

import logging
import pandas as pd
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """SQLAlchemy 占位 Base — 旧代码 declarative_base 兼容。新代码请用 Django ORM。"""
    pass


class _NoopSession:
    """占位 session，all calls 返回 None / 空 query。"""
    def __getattr__(self, name):
        return lambda *a, **kw: None
    def __enter__(self): return self
    def __exit__(self, *exc): pass
    def close(self): pass


class DatabaseManager:
    """No-op 兼容层。所有方法 logger.warning + 返回安全默认值。"""

    SessionLocal = _NoopSession
    engine = None

    def __init__(self, *args, **kwargs):
        logger.warning(
            "services.data.database.DatabaseManager 已废弃；请改用 Django ORM。"
        )

    def init_tables(self):
        logger.debug("DatabaseManager.init_tables: noop")

    def query(self, sql, params=None):
        logger.warning(f"DatabaseManager.query: noop, sql={sql[:80]}...")
        return pd.DataFrame()

    def get_session(self):
        return _NoopSession()

    def get_us_tickers(self, **kwargs):
        return []

    def get_industry_map(self):
        return pd.DataFrame()

    def get_latest_trade_date(self):
        return None

    def get_latest_macro_date(self):
        return None

    def get_latest_commodity_date(self):
        return None

    def upsert(self, *args, **kwargs):
        logger.warning("DatabaseManager.upsert: noop")

    def upsert_df(self, *args, **kwargs):
        logger.warning("DatabaseManager.upsert_df: noop")

    def flush_writes(self):
        pass

    def table_count(self, *args, **kwargs):
        return 0

    def __getattr__(self, name):
        # 任何未知方法返回 noop
        def _noop(*args, **kwargs):
            logger.debug(f"DatabaseManager.{name}: noop")
            return None
        return _noop


# Re-exports for legacy code that does `from services.data.database import USKeyMetric` etc.
# 这些 SQLAlchemy 模型已被 Django ORM 替代，US 旧代码中的引用会拿到 None 占位
USKeyMetric = None
USStockBasic = None
USDailyPrice = None
USFinancialData = None
PaperAccount = None
PaperPosition = None
PaperTransaction = None
PaperNav = None
USPaperAccount = None
USPaperPosition = None
USPaperTransaction = None
USPaperNav = None
