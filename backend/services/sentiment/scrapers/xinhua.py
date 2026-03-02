"""新华社爬虫"""

import re
from typing import Optional

from bs4 import BeautifulSoup

from backend.services.sentiment.base_scraper import BaseScraper


class XinhuaScraper(BaseScraper):
    source = "xinhua"
    source_name = "新华社"
    base_url = "https://www.news.cn"
    tier = 1
    list_urls = [
        "https://www.news.cn/politics/",
        "https://www.news.cn/finance/",
    ]
    encoding = "utf-8"

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        articles = []

        for item in soup.select(".domPC ul li, .tit, .dataList li, .list li"):
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
                "category": "要闻",
            })

        return articles

    def _extract_date(self, item, href: str = "") -> str:
        # 从 span/em 提取
        for span in item.find_all(["span", "em"]):
            text = span.get_text(strip=True)
            m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
            if m:
                return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        # 从 URL 提取日期 (新华社 URL 格式: /20240101/xxx.htm)
        m = re.search(r"/(\d{4})(\d{2})(\d{2})/", href)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        # 从整个文本提取
        text = item.get_text()
        m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        return ""

    def parse_article_page(self, html: str, url: str) -> Optional[dict]:
        soup = BeautifulSoup(html, "lxml")
        content = self._extract_body_text(soup, [
            "#detail", ".article", ".detail", "#content",
        ])
        return {"content": content} if content else None
