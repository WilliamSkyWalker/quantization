"""
东方财富财经快讯爬虫（AKShare 接口）

通过 AKShare 的 stock_info_global_em() 接口获取东方财富全球财经快讯。
"""

import hashlib
import logging
from datetime import datetime

import akshare as ak
import pandas as pd

from services.config import LOG_LEVEL, SENTIMENT_MAX_PAGES
from services.sentiment.base_scraper import BaseScraper

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class EastMoneyScraper(BaseScraper):
    """
    东方财富财经快讯爬虫。

    通过 AKShare 获取东方财富全球财经快讯。
    max_pages 复用为回看天数。
    """

    source = "eastmoney"
    source_name = "东方财富"
    base_url = "https://finance.eastmoney.com"
    tier = 6
    list_urls = []
    fetch_content = False  # AKShare 已返回内容，不需要详情页抓取

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        """东方财富不使用 HTML 列表页，此方法不会被调用。"""
        return []

    def scrape_pages(self, max_pages: int = SENTIMENT_MAX_PAGES, **kwargs):
        """
        抓取生成器：按天分批 yield。

        Args:
            max_pages: 回看天数（复用 max_pages 参数）。

        Yields:
            list[dict] — 当天的文章列表。
        """
        seen_hashes: set[str] = set()

        try:
            df = ak.stock_info_global_em()
        except Exception as e:
            logger.warning(f"[{self.source}] 获取东方财富快讯失败: {e}")
            return

        if df is None or df.empty:
            logger.debug(f"scrape_pages: [{self.source}] 东方财富快讯数据为空，跳过")
            return

        # 标准化列名
        col_map = {}
        for col in df.columns:
            col_lower = col.lower().strip()
            if "标题" in col or "title" in col_lower:
                col_map["title"] = col
            elif "摘要" in col or "内容" in col or "content" in col_lower or "summary" in col_lower:
                col_map["summary"] = col
            elif "时间" in col or "日期" in col or "date" in col_lower or "time" in col_lower:
                col_map["date"] = col
            elif "链接" in col or "url" in col_lower or "link" in col_lower:
                col_map["url"] = col

        # 按列位置兜底：第一列通常为标题
        cols = df.columns.tolist()
        if "title" not in col_map and len(cols) >= 1:
            col_map["title"] = cols[0]
        if "summary" not in col_map and len(cols) >= 2:
            col_map["summary"] = cols[1]
        if "date" not in col_map and len(cols) >= 3:
            col_map["date"] = cols[2]
        if "url" not in col_map and len(cols) >= 4:
            col_map["url"] = cols[3]

        # 解析日期列，按天分组
        today = datetime.now().date()
        day_buckets: dict[str, list[dict]] = {}

        for _, row in df.iterrows():
            title = str(row.get(col_map.get("title", ""), "")).strip()
            summary = str(row.get(col_map.get("summary", ""), "")).strip()
            raw_date = row.get(col_map.get("date", ""), "")
            article_url = str(row.get(col_map.get("url", ""), "")).strip()

            if not title and not summary:
                logger.debug(f"scrape_pages: [{self.source}] 标题和摘要均为空，跳过")
                continue

            if not title:
                title = summary[:100]

            # 解析发布日期
            pub_date = self._parse_date(raw_date, today)
            date_str = pub_date.strftime("%Y-%m-%d")

            # 回看天数过滤
            days_diff = (today - pub_date).days
            if days_diff >= max_pages:
                logger.debug(f"scrape_pages: [{self.source}] 文章 {date_str} 超出回看范围 {max_pages} 天，跳过")
                continue

            content_hash = self._compute_content_hash(title, date_str)
            if content_hash in seen_hashes:
                logger.debug(f"scrape_pages: [{self.source}] 重复内容哈希，跳过: {title[:30]}")
                continue
            seen_hashes.add(content_hash)

            # URL: 优先使用 API 提供的链接
            if not article_url or article_url == "nan":
                title_hash = hashlib.sha256(
                    f"{title}|{date_str}".encode("utf-8")
                ).hexdigest()[:16]
                article_url = f"https://finance.eastmoney.com/a/{title_hash}.html"

            article = {
                "source": self.source,
                "tier": self.tier,
                "title": title[:500],
                "url": article_url,
                "publish_date": date_str,
                "category": "财经快讯",
                "summary": (summary or title)[:2000],
                "content": summary,
                "content_hash": content_hash,
                "scraped_at": datetime.now(),
            }

            day_buckets.setdefault(date_str, []).append(article)

        # 按日期倒序 yield
        for date_str in sorted(day_buckets.keys(), reverse=True):
            yield day_buckets[date_str]

    def scrape(self, max_pages: int = SENTIMENT_MAX_PAGES) -> list[dict]:
        """
        抓取东方财富财经快讯。

        Args:
            max_pages: 回看天数。

        Returns:
            文章字典列表。
        """
        all_articles = []
        for day_articles in self.scrape_pages(max_pages):
            all_articles.extend(day_articles)
        logger.info(f"[{self.source}] {self.source_name} 抓取完成: {len(all_articles)} 篇")
        return all_articles

    @staticmethod
    def _parse_date(raw_date, default_date) -> "datetime.date":
        """尝试解析日期字符串，失败则返回默认日期。"""
        if isinstance(raw_date, (pd.Timestamp, datetime)):
            return raw_date.date() if hasattr(raw_date, "date") else raw_date
        raw_str = str(raw_date).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d", "%m月%d日 %H:%M"):
            try:
                return datetime.strptime(raw_str, fmt).date()
            except (ValueError, TypeError):
                continue
        logger.debug(f"_parse_date: 所有格式均无法解析日期 '{raw_str}'，使用默认值")
        return default_date
