"""中国政府网爬虫 — 最新政策（JSON API）"""

import json
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from services.sentiment.base_scraper import BaseScraper

logger = logging.getLogger(__name__)


class GovCnScraper(BaseScraper):
    source = "gov_cn"
    source_name = "中国政府网"
    base_url = "https://www.gov.cn"
    tier = 1
    # gov.cn 列表页通过 AJAX 加载 JSON，直接请求 JSON 文件
    list_urls = [
        "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json",
    ]
    encoding = "utf-8"

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        """gov.cn 返回 JSON 而非 HTML。"""
        articles = []
        try:
            data = json.loads(html)
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"[{self.source}] JSON 解析失败: {url}")
            return articles

        if not isinstance(data, list):
            return articles

        for item in data:
            title = item.get("TITLE", "").strip()
            article_url = item.get("URL", "").strip()
            pub_date = item.get("DOCRELPUBTIME", "").strip()

            if not title or not article_url or not pub_date:
                continue

            # 日期格式已经是 YYYY-MM-DD
            m = re.match(r"(\d{4}-\d{2}-\d{2})", pub_date)
            if not m:
                continue

            articles.append({
                "title": self._clean_text(title, max_len=500),
                "url": article_url,
                "publish_date": m.group(1),
                "category": "政策",
            })

        return articles

    def parse_article_page(self, html: str, url: str) -> Optional[dict]:
        soup = BeautifulSoup(html, "lxml")
        content = self._extract_body_text(soup, [
            "#UCAP-CONTENT", ".pages_content", ".article", "#content",
        ])
        return {"content": content} if content else None
