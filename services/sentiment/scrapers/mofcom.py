"""商务部爬虫

改版后 /zwgk/zcfb/index.html 仅含导航链接（无日期、无新闻）。
改为抓取 /xwfb/（新闻发布）页，使用 .txtList_01 li 选择器，
日期在 <span>[YYYY-MM-DD]</span> 中。
"""

import re
from typing import Optional

from bs4 import BeautifulSoup

from services.sentiment.base_scraper import BaseScraper


class MofcomScraper(BaseScraper):
    source = "mofcom"
    source_name = "商务部"
    base_url = "https://www.mofcom.gov.cn"
    tier = 2
    list_urls = [
        "https://www.mofcom.gov.cn/xwfb/",      # 新闻发布（含领导活动/日常/发言人等多栏目）
        "https://www.mofcom.gov.cn/",             # 首页（含政策公告）
    ]
    encoding = "utf-8"

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        articles = []
        seen_hrefs: set[str] = set()

        for item in soup.select(".txtList_01 li"):
            link = item.find("a")
            if not link or not link.get("href"):
                continue

            title = self._clean_text(link.get_text())
            if not title or len(title) < 6:
                continue

            href = self._normalize_url(link["href"], url)
            if href in seen_hrefs:
                continue
            # 跳过外站链接（scio.gov.cn 等），抓详情页必 521
            if "mofcom.gov.cn" not in href:
                continue
            seen_hrefs.add(href)

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
        # <span>[YYYY-MM-DD]</span> 格式
        for span in item.find_all(["span", "em"]):
            text = span.get_text(strip=True)
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
            if m:
                return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        # 从整段文本中提取日期
        text = item.get_text(strip=True)
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        # URL: /art/2026/art_xxx.html — 仅有年份，不够精确，跳过
        # URL: /zcfb/20240101/xxx.shtml
        m = re.search(r"/(\d{4})(\d{2})(\d{2})/", href)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        return ""

    def parse_article_page(self, html: str, url: str) -> Optional[dict]:
        soup = BeautifulSoup(html, "lxml")
        content = self._extract_body_text(soup, [
            "#zoom", ".artContent", ".article", ".txtCon",
            ".TRS_Editor", "#content",
        ])
        return {"content": content} if content else None
