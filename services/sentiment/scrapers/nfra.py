"""
金融监管总局爬虫（原银保监会）

通过 NFRA 内部 API (/cbircweb/DocInfo/SelectDocByItemIdAndChild)
获取政策文件列表（JSON），无需 headless browser。
"""

import logging
from datetime import datetime

from services.config import LOG_LEVEL, SENTIMENT_MAX_PAGES
from services.sentiment.base_scraper import BaseScraper

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# NFRA 文件列表 API
_NFRA_API_URL = "https://www.nfra.gov.cn/cbircweb/DocInfo/SelectDocByItemIdAndChild"

# 关注的栏目 itemId（政策法规相关）
_ITEM_IDS = [
    861,   # 政策法规 → 规章
    915,   # 新闻发布
]


class NfraScraper(BaseScraper):
    source = "nfra"
    source_name = "金融监管总局"
    base_url = "https://www.nfra.gov.cn"
    tier = 3
    list_urls = []  # 不使用 HTML 列表页，走 API
    encoding = "utf-8"
    fetch_content = False  # API 返回标题即可，正文为 PDF

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        """不使用 HTML 列表页。"""
        return []

    def scrape_pages(self, max_pages: int = SENTIMENT_MAX_PAGES, **kwargs):
        """
        通过 JSON API 逐页抓取生成器。

        Yields:
            list[dict] — 每页的文章列表。
        """
        seen_ids: set[int] = set()

        for item_id in _ITEM_IDS:
            for page_idx in range(1, max_pages + 1):
                params = {
                    "itemId": item_id,
                    "pageSize": 50,
                    "pageIndex": page_idx,
                }

                try:
                    self.limiter.acquire("www.nfra.gov.cn")
                    resp = self.session.get(
                        _NFRA_API_URL,
                        params=params,
                        timeout=30,
                    )
                    resp.raise_for_status()
                    result = resp.json()
                except Exception as e:
                    logger.warning(f"[{self.source}] API 请求失败 (itemId={item_id}, page={page_idx}): {e}")
                    break

                if result.get("rptCode") != 200:
                    break

                rows = result.get("data", {}).get("rows") or []
                if not rows:
                    break

                page_articles = []
                for row in rows:
                    doc_id = row.get("docId")
                    if not doc_id or doc_id in seen_ids:
                        continue
                    seen_ids.add(doc_id)

                    title = (row.get("docSubtitle") or row.get("docTitle") or "").strip()
                    if not title:
                        continue

                    # 发布日期
                    pub_date_str = row.get("publishDate") or row.get("builddate") or ""
                    pub_date = pub_date_str[:10] if len(pub_date_str) >= 10 else ""
                    if not pub_date:
                        continue

                    # 详情页 URL
                    url = (
                        f"https://www.nfra.gov.cn/cn/view/pages/ItemDetail.html"
                        f"?docId={doc_id}&itemId={item_id}&generaltype=1"
                    )

                    page_articles.append({
                        "source": self.source,
                        "tier": self.tier,
                        "title": title[:500],
                        "url": url,
                        "publish_date": pub_date,
                        "category": "监管政策",
                        "summary": title,
                        "content_hash": self._compute_content_hash(title, pub_date),
                        "scraped_at": datetime.now(),
                    })

                if page_articles:
                    yield page_articles

                # 检查是否还有更多
                total = result.get("data", {}).get("total", 0)
                if page_idx * 50 >= total:
                    break

    def scrape(self, max_pages: int = SENTIMENT_MAX_PAGES) -> list[dict]:
        all_articles = []
        for page_articles in self.scrape_pages(max_pages):
            all_articles.extend(page_articles)
        logger.info(f"[{self.source}] {self.source_name} 抓取完成: {len(all_articles)} 篇")
        return all_articles
