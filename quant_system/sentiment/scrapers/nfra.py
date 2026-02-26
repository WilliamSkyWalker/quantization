"""
金融监管总局爬虫（原银保监会）

注意: NFRA 网站使用 Vue 前端框架，列表数据通过 JS 动态加载，
当前版本无法抓取。后续需接入 headless browser 或找到数据 API。
"""

import re

from bs4 import BeautifulSoup

from sentiment.base_scraper import BaseScraper


class NfraScraper(BaseScraper):
    source = "nfra"
    source_name = "金融监管总局"
    base_url = "https://www.nfra.gov.cn"
    tier = 3
    list_urls = [
        "https://www.nfra.gov.cn/cn/view/pages/index/index.html",
    ]
    encoding = "utf-8"

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        articles = []

        for item in soup.select(
            ".doclist li, .list li, .news-list li, .zhengce-list li"
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
                "category": "监管政策",
            })

        return articles

    def _extract_date(self, item, href: str = "") -> str:
        for span in item.find_all(["span", "em", "td", "div"]):
            text = span.get_text(strip=True)
            m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
            if m:
                return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        m = re.search(r"/(\d{4})(\d{2})(\d{2})/", href)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        text = item.get_text()
        m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        return ""
