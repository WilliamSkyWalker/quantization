"""
舆情数据 ORM 表定义

- PolicyArticle: 政策文章表（抓取的政府网站新闻）
- ScrapeLog: 抓取日志表（每次运行的统计）
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
    UniqueConstraint,
    Index,
)

from backend.services.data.database import Base


class PolicyArticle(Base):
    """政策文章表"""
    __tablename__ = "policy_article"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(50), nullable=False, comment="来源标识 (gov_cn, csrc 等)")
    tier = Column(Integer, nullable=False, comment="1=最高层, 2=产业层, 3=金融监管, 4=专项行业")
    title = Column(String(500), nullable=False, comment="文章标题")
    url = Column(String(500), nullable=False, comment="原文 URL")
    publish_date = Column(Date, nullable=False, comment="发布日期")
    category = Column(String(100), comment="栏目分类")
    summary = Column(String(2000), comment="摘要/前200字")
    content = Column(Text, comment="文章正文全文")
    content_hash = Column(String(64), comment="SHA256(title+date) 跨源去重")
    scraped_at = Column(DateTime, default=datetime.now, comment="抓取时间")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间")

    __table_args__ = (
        UniqueConstraint("url", name="uq_policy_article_url"),
        Index("idx_policy_source", "source"),
        Index("idx_policy_publish_date", "publish_date"),
        Index("idx_policy_tier", "tier"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )


class PolicyAnalysis(Base):
    """政策文章分析结果表"""
    __tablename__ = "policy_analysis"

    id = Column(Integer, primary_key=True, autoincrement=True)
    article_id = Column(Integer, nullable=False, comment="关联 policy_article.id")
    analysis_type = Column(String(20), nullable=False, comment="keyword | llm")
    industries = Column(String(500), comment="逗号分隔的行业列表")
    sentiment = Column(Float, comment="情感分 -1.0 ~ +1.0")
    intensity = Column(Float, comment="影响力/重要性 0.0 ~ 1.0")
    impact_type = Column(String(30), comment="政策影响类型: trade_tariff|tech_sanction|monetary_policy|fiscal_stimulus|industry_regulation|general_policy")
    keywords_hit = Column(String(500), comment="命中的关键词")
    summary_text = Column(String(2000), comment="LLM 分析摘要（可选）")
    affected_stocks = Column(Text, comment="JSON: LLM识别的受影响股票 [{code,name,impact}]")
    analyzed_at = Column(DateTime, default=datetime.now, comment="分析时间")

    __table_args__ = (
        UniqueConstraint("article_id", "analysis_type", name="uq_article_analysis"),
        Index("idx_policy_analysis_article", "article_id"),
        Index("idx_policy_analysis_type", "analysis_type"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )


class ScrapeLog(Base):
    """抓取日志表"""
    __tablename__ = "scrape_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(50), nullable=False, comment="来源标识")
    started_at = Column(DateTime, comment="开始时间")
    finished_at = Column(DateTime, comment="结束时间")
    articles_found = Column(Integer, default=0, comment="发现文章数")
    articles_new = Column(Integer, default=0, comment="新增文章数")
    status = Column(String(20), default="running", comment="running/success/failed")
    error_message = Column(String(1000), comment="错误信息")
