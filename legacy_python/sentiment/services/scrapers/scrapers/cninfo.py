"""
巨潮资讯网上市公司公告爬虫

通过巨潮资讯网 API 获取最近上市公司公告标题。
公告本身是 PDF，不解析全文，仅用标题进行关键词分析。
"""

import json
import logging
import re
from datetime import datetime, timedelta

from services.config import LOG_LEVEL, SENTIMENT_MAX_PAGES
from sentiment.services.scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# 巨潮公告查询 API
CNINFO_API_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"

# 沪深A股: 深交所 + 上交所
_A_SHARE_COLUMNS = ["szse", "sse"]
# A股代码正则（排除B股 200xxx/900xxx）
_A_SHARE_CODE_RE = re.compile(r"^(00[0-3]|300|30[1-9]|60[0135])\d{3}$")


class CninfoScraper(BaseScraper):
    """
    巨潮资讯网上市公司公告爬虫。

    通过 cninfo POST API 获取最近公告列表，
    公告标题写入 policy_article 供 keyword 分析。
    """

    source = "cninfo"
    source_name = "巨潮公告"
    base_url = "http://www.cninfo.com.cn"
    tier = 2
    list_urls = []
    fetch_content = False  # 公告为 PDF，不抓取全文

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        """Cninfo 不使用 HTML 列表页，此方法不会被调用。"""
        return []

    def scrape_pages(self, max_pages: int = SENTIMENT_MAX_PAGES, **kwargs):
        """
        逐日窗口抓取生成器：按 7 天为一个时间窗口分批拉取公告。

        Args:
            max_pages: 回看天数（复用参数名，与 CCTV 行为一致）。

        Yields:
            list[dict] — 当前窗口的文章列表。
        """
        seen_urls: set[str] = set()
        lookback_days = max_pages  # 复用 max_pages 作为回看天数

        now = datetime.now()
        # 以 7 天为窗口向前滚动
        window_days = 7
        windows = []
        for offset in range(0, lookback_days, window_days):
            w_end = now - timedelta(days=offset)
            w_start = now - timedelta(days=min(offset + window_days, lookback_days))
            windows.append((w_start, w_end))

        for w_start, w_end in windows:
            se_date = f"{w_start.strftime('%Y-%m-%d')}~{w_end.strftime('%Y-%m-%d')}"

            for column in _A_SHARE_COLUMNS:
                page_num = 1

                while True:
                    data = {
                        "pageNum": page_num,
                        "pageSize": 30,
                        "column": column,
                        "tabName": "fulltext",
                        "seDate": se_date,
                        "isHLtitle": "true",
                    }

                    try:
                        self.limiter.acquire("www.cninfo.com.cn")
                        resp = self.session.post(
                            CNINFO_API_URL,
                            data=data,
                            timeout=30,
                        )
                        resp.raise_for_status()
                        result = resp.json()
                    except Exception as e:
                        logger.warning(f"[{self.source}] 巨潮公告 API 请求失败 ({column} {se_date} 页{page_num}): {e}")
                        break

                    announcements = result.get("announcements") or []
                    if not announcements:
                        logger.debug(f"scrape_pages: [{self.source}] {column} {se_date} 页{page_num} 无公告，结束翻页")
                        break

                    page_articles = []
                    for ann in announcements:
                        # 只保留沪深A股代码
                        sec_code = ann.get("secCode") or ""
                        if sec_code and not _A_SHARE_CODE_RE.match(sec_code):
                            logger.debug(f"scrape_pages: [{self.source}] 非A股代码 {sec_code}，跳过")
                            continue

                        title = (ann.get("announcementTitle") or "").strip()
                        title = title.replace("<em>", "").replace("</em>", "")
                        if not title:
                            logger.debug(f"scrape_pages: [{self.source}] 公告标题为空，跳过")
                            continue

                        adjunct_url = ann.get("adjunctUrl") or ""
                        if adjunct_url:
                            full_url = f"http://static.cninfo.com.cn/{adjunct_url}"
                        else:
                            logger.debug(f"scrape_pages: [{self.source}] 公告无附件URL，跳过")
                            continue

                        if full_url in seen_urls:
                            logger.debug(f"scrape_pages: [{self.source}] URL 重复，跳过: {full_url}")
                            continue
                        seen_urls.add(full_url)

                        ann_time = ann.get("announcementTime")
                        if ann_time:
                            try:
                                pub_date = datetime.fromtimestamp(
                                    ann_time / 1000
                                ).strftime("%Y-%m-%d")
                            except (ValueError, TypeError, OSError):
                                pub_date = w_end.strftime("%Y-%m-%d")
                        else:
                            pub_date = w_end.strftime("%Y-%m-%d")

                        sec_name = ann.get("secName") or ""
                        summary = f"[{sec_code} {sec_name}] {title}" if sec_code else title

                        content_hash = self._compute_content_hash(title, pub_date)

                        page_articles.append({
                            "source": self.source,
                            "tier": self.tier,
                            "title": title[:500],
                            "url": full_url,
                            "publish_date": pub_date,
                            "category": "上市公司公告",
                            "summary": summary[:2000],
                            "content_hash": content_hash,
                            "scraped_at": datetime.now(),
                        })

                    if page_articles:
                        yield page_articles

                    total_ann = result.get("totalAnnouncement") or 0
                    if page_num * 30 >= total_ann:
                        logger.debug(f"scrape_pages: [{self.source}] {column} {se_date} 已翻完所有页 (total={total_ann})")
                        break
                    page_num += 1

    def scrape(self, max_pages: int = SENTIMENT_MAX_PAGES) -> list[dict]:
        """
        抓取巨潮公告列表。

        Args:
            max_pages: 最大翻页数。

        Returns:
            文章字典列表。
        """
        all_articles = []
        for page_articles in self.scrape_pages(max_pages):
            all_articles.extend(page_articles)
        logger.info(f"[{self.source}] {self.source_name} 抓取完成: {len(all_articles)} 篇")
        return all_articles
