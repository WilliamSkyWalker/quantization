"""
CCTV 新闻联播爬虫（AKShare 接口）

通过 AKShare 的 news_cctv() 接口获取新闻联播文字稿。
akshare 未安装时报错（akshare 为必需依赖）。
"""

import hashlib
import logging
from datetime import datetime, timedelta

import akshare as ak

from services.config import LOG_LEVEL, SENTIMENT_MAX_PAGES
from services.sentiment.base_scraper import BaseScraper

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class CCTVScraper(BaseScraper):
    """
    CCTV 新闻联播爬虫。

    通过 AKShare 获取指定日期的新闻联播文字稿。
    max_pages 复用为回看天数。
    """

    source = "cctv"
    source_name = "CCTV新闻联播"
    base_url = "https://tv.cctv.com"
    tier = 1
    list_urls = []
    fetch_content = False  # AKShare 已返回全文，不需要详情页抓取

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        """CCTV 不使用 HTML 列表页，此方法不会被调用。"""
        return []

    def scrape_pages(self, max_pages: int = SENTIMENT_MAX_PAGES, **kwargs):
        """
        逐日抓取生成器：每天的新闻联播作为一批 yield。

        Args:
            max_pages: 回看天数（复用 max_pages 参数）。

        Yields:
            list[dict] — 当天的文章列表。
        """
        seen_hashes: set[str] = set()
        today = datetime.now().date()

        for days_ago in range(max_pages):
            target_date = today - timedelta(days=days_ago)
            date_str = target_date.strftime("%Y%m%d")

            try:
                df = ak.news_cctv(date=date_str)
            except Exception as e:
                logger.warning(f"[{self.source}] 获取 {date_str} 新闻联播失败: {e}")
                continue

            if df is None or df.empty:
                logger.debug(f"scrape_pages: [{self.source}] {date_str} 无新闻联播数据，跳过")
                continue

            day_articles = []
            for _, row in df.iterrows():
                title = str(row.get("title", "")).strip()
                content = str(row.get("content", "")).strip()

                if not title:
                    logger.debug(f"scrape_pages: [{self.source}] {date_str} 某条新闻标题为空，跳过")
                    continue

                # 生成唯一 URL（基于日期和标题哈希）
                title_hash = hashlib.sha256(
                    f"{title}|{date_str}".encode("utf-8")
                ).hexdigest()[:16]
                url = f"https://tv.cctv.com/lm/xwlb/{date_str}/{title_hash}.shtml"

                content_hash = self._compute_content_hash(title, target_date.strftime("%Y-%m-%d"))

                if content_hash in seen_hashes:
                    logger.debug(f"scrape_pages: [{self.source}] 重复内容哈希，跳过: {title[:30]}")
                    continue
                seen_hashes.add(content_hash)

                # summary 填充正文内容（截断 2000 字），供 keyword/LLM 分析使用
                summary = content[:2000] if content else title

                day_articles.append({
                    "source": self.source,
                    "tier": self.tier,
                    "title": title[:500],
                    "url": url,
                    "publish_date": target_date.strftime("%Y-%m-%d"),
                    "category": "新闻联播",
                    "summary": summary,
                    "content": content,
                    "content_hash": content_hash,
                    "scraped_at": datetime.now(),
                })

            if day_articles:
                yield day_articles

    def scrape(self, max_pages: int = SENTIMENT_MAX_PAGES) -> list[dict]:
        """
        抓取新闻联播文字稿。

        Args:
            max_pages: 回看天数（复用 max_pages 参数）。

        Returns:
            文章字典列表。
        """
        all_articles = []
        for day_articles in self.scrape_pages(max_pages):
            all_articles.extend(day_articles)
        logger.info(f"[{self.source}] {self.source_name} 抓取完成: {len(all_articles)} 篇")
        return all_articles
