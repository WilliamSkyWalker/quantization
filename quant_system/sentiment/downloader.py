"""
舆情数据下载编排器

负责调度所有爬虫，写入数据库，记录抓取日志。
"""

import logging
from datetime import datetime

from config.settings import LOG_LEVEL, SENTIMENT_MAX_PAGES
from data.database import DatabaseManager
from sentiment.scrapers import SCRAPER_REGISTRY, TIER_MAP
from sentiment.base_scraper import HttpRateLimiter

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class SentimentDownloader:
    """
    舆情数据下载编排器。

    用法:
        db = DatabaseManager()
        db.init_tables()
        dl = SentimentDownloader(db)
        dl.download_all()
    """

    def __init__(self, db: DatabaseManager):
        self.db = db
        self.limiter = HttpRateLimiter()

    def download_all(self, max_pages: int = SENTIMENT_MAX_PAGES) -> dict:
        """
        全量抓取所有来源。

        Returns:
            {source: {"found": int, "new": int, "status": str}, ...}
        """
        results = {}
        for source_id in SCRAPER_REGISTRY:
            results[source_id] = self.download_source(source_id, max_pages)
        return results

    def download_source(self, source: str, max_pages: int = SENTIMENT_MAX_PAGES) -> dict:
        """
        抓取单个来源。

        Args:
            source: 来源标识，如 "gov_cn"。
            max_pages: 最大翻页数。

        Returns:
            {"found": int, "new": int, "status": str}
        """
        if source not in SCRAPER_REGISTRY:
            logger.error(f"未知来源: {source}，可用: {list(SCRAPER_REGISTRY.keys())}")
            return {"found": 0, "new": 0, "status": "failed"}

        scraper_cls = SCRAPER_REGISTRY[source]
        scraper = scraper_cls(limiter=self.limiter)

        started_at = datetime.now()

        try:
            articles = scraper.scrape(max_pages=max_pages)
            found = len(articles)

            # 写入数据库
            new_count = 0
            if articles:
                new_count = self.db.upsert_policy_articles(articles)

            # 记录抓取日志
            self.db.upsert_scrape_log({
                "source": source,
                "started_at": started_at,
                "finished_at": datetime.now(),
                "articles_found": found,
                "articles_new": new_count,
                "status": "success",
            })

            logger.info(
                f"[{source}] {scraper.source_name}: "
                f"发现 {found} 篇，新增 {new_count} 篇"
            )
            return {"found": found, "new": new_count, "status": "success"}

        except Exception as e:
            self.db.upsert_scrape_log({
                "source": source,
                "started_at": started_at,
                "finished_at": datetime.now(),
                "articles_found": 0,
                "articles_new": 0,
                "status": "failed",
                "error_message": str(e)[:1000],
            })

            logger.error(f"[{source}] 抓取失败: {e}")
            return {"found": 0, "new": 0, "status": "failed"}

    def download_tier(self, tier: int, max_pages: int = SENTIMENT_MAX_PAGES) -> dict:
        """
        按层级抓取。

        Args:
            tier: 1=最高层, 2=产业层, 3=金融监管, 4=专项行业。

        Returns:
            {source: {"found": int, "new": int, "status": str}, ...}
        """
        sources = TIER_MAP.get(tier, [])
        if not sources:
            logger.error(f"未知层级: {tier}，可用: {list(TIER_MAP.keys())}")
            return {}

        results = {}
        for source_id in sources:
            results[source_id] = self.download_source(source_id, max_pages)
        return results
