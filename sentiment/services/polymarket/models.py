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
from sqlalchemy.dialects.postgresql import JSONB

from services.data.database import Base


class PolymarketEvent(Base):
    """Polymarket 监控事件/市场（一行 = 一个 market，多 markets 共享 event_id）"""
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
    # ===== 扩展字段 (Gamma API 全字段) =====
    # Event 层
    event_id = Column(String(50), comment="Gamma event ID（多 market 共享）")
    event_ticker = Column(String(200), comment="Event ticker/slug")
    title = Column(String(1000), comment="Event 标题")
    tags = Column(JSONB, comment="JSON: event tags")
    open_interest = Column(Float, comment="未平仓金额 (USD)")
    volume_1wk = Column(Float, comment="近 1 周成交量")
    volume_1mo = Column(Float, comment="近 1 月成交量")
    volume_1yr = Column(Float, comment="近 1 年成交量")
    neg_risk = Column(Boolean, comment="是否 neg risk")
    neg_risk_market_id = Column(String(100), comment="Neg risk market ID")
    comment_count = Column(Integer, comment="评论数")
    closed_time = Column(DateTime, comment="实际关闭时间")
    start_date = Column(DateTime, comment="开始时间")
    restricted = Column(Boolean, comment="是否受限")
    archived = Column(Boolean, comment="是否归档")
    # Market 层
    outcomes = Column(JSONB, comment="JSON: outcome 名称列表 ['Yes','No']")
    outcome_prices = Column(JSONB, comment="JSON: outcome 价格列表 ['1','0']")
    best_bid = Column(Float, comment="最佳买价")
    best_ask = Column(Float, comment="最佳卖价")
    spread = Column(Float, comment="买卖价差")
    last_trade_price = Column(Float, comment="最近成交价")
    volume_clob = Column(Float, comment="CLOB 真实成交量")
    volume_num = Column(Float, comment="数值化 volume")
    one_day_price_change = Column(Float, comment="近 1 天价格变动")
    one_hour_price_change = Column(Float, comment="近 1 小时价格变动")
    one_week_price_change = Column(Float, comment="近 1 周价格变动")
    one_month_price_change = Column(Float, comment="近 1 月价格变动")
    one_year_price_change = Column(Float, comment="近 1 年价格变动")
    uma_bond = Column(String(50), comment="UMA 押金")
    uma_reward = Column(String(50), comment="UMA 奖励")
    maker_base_fee = Column(Float, comment="Maker 基础手续费")
    taker_base_fee = Column(Float, comment="Taker 基础手续费")
    market_type = Column(String(50), comment="market type")
    market_closed = Column(Boolean, comment="market 是否关闭")
    market_active = Column(Boolean, comment="market 是否活跃")
    # ===== 时间戳 =====
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
