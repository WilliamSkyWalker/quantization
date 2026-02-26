"""商务部爬虫"""

import re

from bs4 import BeautifulSoup

from sentiment.base_scraper import BaseScraper


class MofcomScraper(BaseScraper):
    source = "mofcom"
    source_name = "商务部"
    base_url = "http://www.mofcom.gov.cn"
    tier = 2
    list_urls = [
        "http://www.mofcom.gov.cn/zwgk/zcfb/index.html",
        "http://www.mofcom.gov.cn/",
    ]
    encoding = "utf-8"

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        articles = []

        # 商务部: ul.txtList_02 li > a, 或通用 li > a
        for item in soup.select(
            ".txtList_02 li, .txtList li, .listCon li"
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
                "category": "政策发布",
            })

        return articles

    def _extract_date(self, item, href: str = "") -> str:
        for span in item.find_all(["span", "em"]):
            text = span.get_text(strip=True)
            m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
            if m:
                return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        # URL: /art/2026/art_xxx.html
        m = re.search(r"/art/(\d{4})/", href)
        if m:
            return f"{m.group(1)}-01-01"

        # URL: /article/zcfb/20240101/xxx.shtml
        m = re.search(r"/(\d{4})(\d{2})(\d{2})/", href)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        text = item.get_text()
        m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        return ""
