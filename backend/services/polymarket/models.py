"""
Polymarket ORM 表定义

- PolymarketEvent: 监控的预测市场事件
- PolymarketPriceSnapshot: 赔率时间序列快照
- PolymarketAlert: 生成的告警（含 LLM 分析）
"""

from datetime import datetime

from sqlalchemy import (
    Column,
    Float,
    Integer,
    String,
    Text,
    Date,
    DateTime,
    Boolean,
    UniqueConstraint,
    Index,
)

from backend.services.data.database import Base


class PolymarketEvent(Base):
    """Polymarket 监控事件/市场"""
    __tablename__ = "polymarket_event"

    id = Column(Integer, primary_key=True, autoincrement=True)
    condition_id = Column(String(100), nullable=False, comment="Polymarket condition ID")
    token_id = Column(String(100), comment="CLOB token ID (YES outcome)")
    question = Column(String(1000), nullable=False, comment="市场问题")
    description = Column(Text, comment="市场描述")
    category = Column(String(100), comment="分类 (politics, economics 等)")
    outcome_yes_price = Column(Float, comment="YES 赔率 (0-1)")
    outcome_no_price = Column(Float, comment="NO 赔率 (0-1)")
    volume = Column(Float, comment="总交易量 (USD)")
    liquidity = Column(Float, comment="流动性 (USD)")
    end_date = Column(DateTime, comment="市场结束日期")
    is_active = Column(Boolean, default=True, comment="是否在监控中")
    is_excluded = Column(Boolean, default=False, comment="软删除：排除的分类(sports/pop-culture等)")
    slug = Column(String(500), comment="Polymarket URL slug")
    gamma_market_id = Column(String(100), comment="Gamma API market ID")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("condition_id", name="uq_polymarket_event_condition"),
        Index("idx_polymarket_event_active", "is_active"),
        Index("idx_polymarket_event_category", "category"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )


class PolymarketPriceSnapshot(Base):
    """赔率时间序列快照"""
    __tablename__ = "polymarket_price_snapshot"

    id = Column(Integer, primary_key=True, autoincrement=True)
    condition_id = Column(String(100), nullable=False, comment="关联 condition_id")
    timestamp = Column(DateTime, nullable=False, comment="快照时间")
    yes_price = Column(Float, comment="YES 赔率")
    no_price = Column(Float, comment="NO 赔率")
    spread = Column(Float, comment="买卖价差")
    volume_24h = Column(Float, comment="24h 交易量")
    source = Column(String(20), default="websocket", comment="数据来源: websocket|gamma")

    __table_args__ = (
        Index("idx_pm_snapshot_condition", "condition_id"),
        Index("idx_pm_snapshot_time", "timestamp"),
        Index("idx_pm_snapshot_condition_time", "condition_id", "timestamp"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )


class PolymarketAlert(Base):
    """Polymarket 事件告警"""
    __tablename__ = "polymarket_alert"

    id = Column(Integer, primary_key=True, autoincrement=True)
    condition_id = Column(String(100), nullable=False, comment="关联 condition_id")
    alert_type = Column(String(20), nullable=False, comment="spike_5m | spike_1h | spike_24h")
    price_before = Column(Float, comment="变动前赔率")
    price_after = Column(Float, comment="变动后赔率")
    price_change = Column(Float, comment="赔率变动 (signed)")
    timeframe_seconds = Column(Integer, comment="时间窗口（秒）")
    question = Column(String(1000), comment="事件问题（冗余存储，便于查询）")
    affected_tickers = Column(Text, comment="JSON: LLM 分析的受影响美股")
    affected_a_shares = Column(Text, comment="JSON: LLM 分析的受影响A股")
    affected_sectors = Column(Text, comment="JSON: 受影响 GICS 行业")
    affected_sw_industries = Column(Text, comment="JSON: 受影响申万一级行业")
    llm_summary = Column(Text, comment="LLM 分析摘要")
    llm_sentiment = Column(Float, comment="LLM 情感倾向 -1.0 ~ +1.0")
    llm_confidence = Column(Float, comment="LLM 置信度 0.0 ~ 1.0")
    is_read = Column(Boolean, default=False, comment="是否已读")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("idx_pm_alert_condition", "condition_id"),
        Index("idx_pm_alert_type", "alert_type"),
        Index("idx_pm_alert_created", "created_at"),
        Index("idx_pm_alert_read", "is_read"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )
