"""
舆情数据下载编排器

负责调度所有爬虫，写入数据库，记录抓取日志。
"""

import logging
from datetime import datetime

from backend.services.config import LOG_LEVEL, SENTIMENT_MAX_PAGES, SENTIMENT_BACKFILL_DAYS
from backend.services.data.database import DatabaseManager
from backend.services.sentiment.scrapers import SCRAPER_REGISTRY, TIER_MAP
from backend.services.sentiment.base_scraper import HttpRateLimiter

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

    def _calc_incremental_days(self, source: str, overlap_days: int = 3) -> int:
        """
        根据数据库中最新文章日期计算增量回看天数。

        Args:
            source: 来源标识。
            overlap_days: 重叠天数，防止边界遗漏。

        Returns:
            回看天数（至少 1 天）。
        """
        last_date = self.db.get_latest_scrape_date(source)
        if last_date is None:
            # 无历史数据，使用全量补录天数
            logger.info(f"[{source}] 无历史数据，自动切换为全量补录 ({SENTIMENT_BACKFILL_DAYS} 天)")
            return SENTIMENT_BACKFILL_DAYS
        try:
            last_dt = datetime.strptime(str(last_date)[:10], "%Y-%m-%d").date()
        except ValueError:
            return SENTIMENT_MAX_PAGES
        days_back = (datetime.now().date() - last_dt).days + overlap_days
        days_back = max(days_back, 1)
        logger.info(f"[{source}] 增量模式：最新文章 {last_date}，回看 {days_back} 天")
        return days_back

    def download_all(self, max_pages: int = SENTIMENT_MAX_PAGES, incremental: bool = False) -> dict:
        """
        全量抓取所有来源。

        Args:
            max_pages: 最大翻页/回看天数。incremental=True 时忽略此参数。
            incremental: 增量模式，自动计算每个来源的回看天数。

        Returns:
            {source: {"found": int, "new": int, "status": str}, ...}
        """
        results = {}
        for source_id in SCRAPER_REGISTRY:
            results[source_id] = self.download_source(source_id, max_pages, incremental=incremental)
        return results

    def download_source(self, source: str, max_pages: int = SENTIMENT_MAX_PAGES, incremental: bool = False) -> dict:
        """
        抓取单个来源（逐页入库）。

        Args:
            source: 来源标识，如 "gov_cn"。
            max_pages: 最大翻页/回看天数。incremental=True 时自动计算。
            incremental: 增量模式，根据数据库最新日期自动确定回看范围。

        Returns:
            {"found": int, "new": int, "status": str}
        """
        if incremental:
            max_pages = self._calc_incremental_days(source)
        if source not in SCRAPER_REGISTRY:
            logger.error(f"未知来源: {source}，可用: {list(SCRAPER_REGISTRY.keys())}")
            return {"found": 0, "new": 0, "status": "failed"}

        scraper_cls = SCRAPER_REGISTRY[source]
        scraper = scraper_cls(limiter=self.limiter)

        started_at = datetime.now()

        try:
            found = 0
            new_count = 0

            # scrape_pages() 由基类和 CCTV/cninfo 实现，逐页入库。
            # Twitter 等只 override scrape() 的爬虫走 fallback 一次性入库。
            has_pages = False
            for page_articles in scraper.scrape_pages(max_pages=max_pages):
                has_pages = True
                found += len(page_articles)
                if page_articles:
                    new_count += self.db.upsert_policy_articles(page_articles)

            if not has_pages and not scraper.list_urls:
                # Fallback: 爬虫未实现 scrape_pages()，调用 scrape()
                articles = scraper.scrape(max_pages=max_pages)
                found = len(articles)
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

    def backfill_content(
        self,
        source: str | None = None,
        batch_size: int = 100,
    ) -> dict:
        """
        补录正文：对已入库但 content 为空的文章抓取详情页。

        Args:
            source: 可选，只补录指定来源。
            batch_size: 每批处理数量。

        Returns:
            {"total": int, "success": int, "failed": int}
        """
        started_at = datetime.now()

        articles = self.db.get_articles_without_content(
            source=source, limit=batch_size,
        )
        if not articles:
            logger.info("补录全文: 没有待补录的文章")
            return {"total": 0, "success": 0, "failed": 0}

        logger.info(f"补录全文开始: 待处理 {len(articles)} 篇")

        success = 0
        failed = 0
        # 缓存爬虫实例，同一来源复用
        scraper_cache: dict = {}

        for i, row in enumerate(articles, 1):
            src = row["source"]
            url = row["url"]
            article_id = row["id"]

            if src not in SCRAPER_REGISTRY:
                logger.warning(f"[{src}] 未知来源，跳过: {url}")
                failed += 1
                continue

            if src not in scraper_cache:
                scraper_cls = SCRAPER_REGISTRY[src]
                scraper = scraper_cls(limiter=self.limiter)
                scraper_cache[src] = scraper
            else:
                scraper = scraper_cache[src]

            # 跳过不支持详情页抓取的爬虫
            if not scraper.fetch_content:
                continue

            html = scraper.fetch_page(url)
            if not html:
                logger.debug(f"[{src}] ({i}/{len(articles)}) 页面抓取失败: {url}")
                failed += 1
                continue

            try:
                result = scraper.parse_article_page(html, url)
            except Exception as e:
                logger.warning(f"[{src}] ({i}/{len(articles)}) 详情页解析失败 {url}: {e}")
                failed += 1
                continue

            content = result.get("content", "") if result else ""
            if content:
                self.db.update_article_content(article_id, content)
                success += 1
                logger.debug(f"[{src}] ({i}/{len(articles)}) 补录成功 ({len(content)}字): {url}")
            else:
                logger.debug(f"[{src}] ({i}/{len(articles)}) 未提取到正文: {url}")
                failed += 1

        # 记录抓取日志
        self.db.upsert_scrape_log({
            "source": source or "backfill_content",
            "started_at": started_at,
            "finished_at": datetime.now(),
            "articles_found": len(articles),
            "articles_new": success,
            "status": "success" if failed == 0 else "partial",
            "error_message": f"成功 {success}, 失败 {failed}" if failed > 0 else None,
        })

        logger.info(
            f"补录全文完成: 总计 {len(articles)}, 成功 {success}, 失败 {failed}"
        )
        return {"total": len(articles), "success": success, "failed": failed}

    def download_tier(self, tier: int, max_pages: int = SENTIMENT_MAX_PAGES, incremental: bool = False) -> dict:
        """
        按层级抓取。

        Args:
            tier: 1=最高层, 2=产业层, 3=金融监管, 4=专项行业。
            max_pages: 最大翻页/回看天数。incremental=True 时自动计算。
            incremental: 增量模式。

        Returns:
            {source: {"found": int, "new": int, "status": str}, ...}
        """
        sources = TIER_MAP.get(tier, [])
        if not sources:
            logger.error(f"未知层级: {tier}，可用: {list(TIER_MAP.keys())}")
            return {}

        results = {}
        for source_id in sources:
            results[source_id] = self.download_source(source_id, max_pages, incremental=incremental)
        return results
