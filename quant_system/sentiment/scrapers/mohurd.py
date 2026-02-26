"""住建部爬虫"""

import re

from bs4 import BeautifulSoup

from sentiment.base_scraper import BaseScraper


class MohurdScraper(BaseScraper):
    source = "mohurd"
    source_name = "住建部"
    base_url = "https://www.mohurd.gov.cn"
    tier = 4
    # jsyw/index.html 是 JS 渲染，/xinwen/ 有静态内容
    list_urls = [
        "https://www.mohurd.gov.cn/xinwen/",
    ]
    encoding = "utf-8"

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        articles = []

        # 住建部结构: li.date > a
        for item in soup.find_all("li"):
            link = item.find("a")
            if not link or not link.get("href"):
                continue

            title = self._clean_text(link.get_text())
            if not title or len(title) < 4:
                continue

            href = self._normalize_url(link["href"], url)

            # 只要住建部自身的链接，排除外部新华社等链接
            if "mohurd.gov.cn" not in href and not href.startswith("/"):
                continue

            pub_date = self._extract_date(item, href)
            if not pub_date:
                continue

            articles.append({
                "title": title,
                "url": href,
                "publish_date": pub_date,
                "category": "住建政策",
            })

        return articles

    def _extract_date(self, item, href: str = "") -> str:
        for span in item.find_all(["span", "em", "td"]):
            text = span.get_text(strip=True)
            m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
            if m:
                return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        # URL: /art/2026/art_xxx.html
        m = re.search(r"/art/(\d{4})/", href)
        if m:
            # 只有年份，用当月1日
            return f"{m.group(1)}-01-01"

        m = re.search(r"/(\d{4})(\d{2})(\d{2})", href)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        text = item.get_text()
        m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        return ""
