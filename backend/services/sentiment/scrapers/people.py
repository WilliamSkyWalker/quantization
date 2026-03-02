"""人民日报爬虫"""

import re
from typing import Optional

from bs4 import BeautifulSoup

from backend.services.sentiment.base_scraper import BaseScraper


class PeopleScraper(BaseScraper):
    source = "people"
    source_name = "人民日报"
    base_url = "http://www.people.com.cn"
    tier = 1
    list_urls = [
        "http://politics.people.com.cn/GB/1026/index.html",
        "http://finance.people.com.cn/",
    ]
    encoding = "utf-8"

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        articles = []

        for item in soup.select(".headingNews a, .hdNews a, .ej_list_box li a, .fl a"):
            if not item.get("href"):
                continue

            title = self._clean_text(item.get_text())
            if not title or len(title) < 4:
                continue

            href = self._normalize_url(item["href"], url)

            pub_date = self._extract_date_from_url(href)
            if not pub_date:
                continue

            articles.append({
                "title": title,
                "url": href,
                "publish_date": pub_date,
                "category": "要闻",
            })

        return articles

    def _extract_date_from_url(self, href: str) -> str:
        """人民日报 URL 通常含日期: /n1/2024/0101/c1001-xxx.html"""
        m = re.search(r"/(\d{4})/(\d{2})(\d{2})/", href)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m = re.search(r"/(\d{4})(\d{2})(\d{2})", href)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return ""

    def parse_article_page(self, html: str, url: str) -> Optional[dict]:
        soup = BeautifulSoup(html, "lxml")
        content = self._extract_body_text(soup, [
            ".rm_txt_con", "#rwb_zw", ".article", "#content",
        ])
        return {"content": content} if content else None
