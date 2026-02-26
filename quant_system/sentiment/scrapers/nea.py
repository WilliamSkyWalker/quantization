"""能源局爬虫"""

import re

from bs4 import BeautifulSoup

from sentiment.base_scraper import BaseScraper


class NeaScraper(BaseScraper):
    source = "nea"
    source_name = "能源局"
    base_url = "http://www.nea.gov.cn"
    tier = 4
    list_urls = [
        "http://www.nea.gov.cn/xwzx/index.htm",
        "http://www.nea.gov.cn/news/index.htm",
    ]
    encoding = "utf-8"

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        articles = []

        # NEA 结构: li > a + span(date), 或 div.xwzx-yw-tit > a
        for item in soup.find_all("li"):
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
                "category": "能源政策",
            })

        return articles

    def _extract_date(self, item, href: str = "") -> str:
        # 查找 span 中的括号日期: (2025-12-17)
        for span in item.find_all(["span", "em"]):
            text = span.get_text(strip=True)
            m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
            if m:
                return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        # URL 日期: /2024-01/01/c_xxx.htm
        m = re.search(r"/(\d{4})-(\d{2})/(\d{2})/", href)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m = re.search(r"/(\d{4})(\d{2})(\d{2})\w+/", href)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        return ""
