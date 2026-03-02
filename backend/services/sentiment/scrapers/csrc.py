"""证监会爬虫"""

import re
from typing import Optional

from bs4 import BeautifulSoup

from backend.services.sentiment.base_scraper import BaseScraper


class CsrcScraper(BaseScraper):
    source = "csrc"
    source_name = "证监会"
    base_url = "https://www.csrc.gov.cn"
    tier = 3
    list_urls = [
        "https://www.csrc.gov.cn/csrc/c100028/common_list.shtml",
        "https://www.csrc.gov.cn/csrc/c100029/common_list.shtml",
    ]
    encoding = "utf-8"

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        articles = []

        for item in soup.select(
            ".list li, ul.list_con li, .common-list li, .news-list li"
        ):
            link = item.find("a")
            if not link or not link.get("href"):
                continue

            title = self._clean_text(link.get_text())
            if not title or len(title) < 4:
                continue

            href = self._normalize_url(link["href"], url)

            pub_date = self._extract_date(item, href)
            if not pub_date:
                continue

            articles.append({
                "title": title,
                "url": href,
                "publish_date": pub_date,
                "category": "监管动态",
            })

        return articles

    def _extract_date(self, item, href: str = "") -> str:
        for span in item.find_all(["span", "em", "td"]):
            text = span.get_text(strip=True)
            m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
            if m:
                return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        # URL 日期模式: /c1286xxx/202401/t20240101_xxx.shtml
        m = re.search(r"/t(\d{4})(\d{2})(\d{2})_", href)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m = re.search(r"/(\d{4})(\d{2})/", href)
        if m:
            return f"{m.group(1)}-{m.group(2)}-01"

        text = item.get_text()
        m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        return ""

    def parse_article_page(self, html: str, url: str) -> Optional[dict]:
        soup = BeautifulSoup(html, "lxml")
        content = self._extract_body_text(soup, [
            "#ContentRegion", ".article_content", ".TRS_Editor", "#content",
        ])
        return {"content": content} if content else None
