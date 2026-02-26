"""发改委爬虫"""

import re

from bs4 import BeautifulSoup

from sentiment.base_scraper import BaseScraper


class NdrcScraper(BaseScraper):
    source = "ndrc"
    source_name = "发改委"
    base_url = "https://www.ndrc.gov.cn"
    tier = 2
    list_urls = [
        "https://www.ndrc.gov.cn/xxgk/zcfb/tz/",
        "https://www.ndrc.gov.cn/xwdt/xwfb/",
    ]
    encoding = "utf-8"

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        articles = []

        for item in soup.select(".u-list li, ul.list li, .list_con li"):
            link = item.find("a")
            if not link or not link.get("href"):
                continue

            title = self._clean_text(link.get_text())
            if not title or len(title) < 4:
                continue

            href = self._normalize_url(link["href"], url)

            pub_date = self._extract_date(item)
            if not pub_date:
                continue

            articles.append({
                "title": title,
                "url": href,
                "publish_date": pub_date,
                "category": "政策发布",
            })

        return articles

    def _extract_date(self, item) -> str:
        for span in item.find_all(["span", "em", "td"]):
            text = span.get_text(strip=True)
            m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
            if m:
                return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        text = item.get_text()
        m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        # 发改委 URL 中也含日期
        link = item.find("a")
        if link and link.get("href"):
            m = re.search(r"/t(\d{4})(\d{2})(\d{2})_", link["href"])
            if m:
                return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        return ""
