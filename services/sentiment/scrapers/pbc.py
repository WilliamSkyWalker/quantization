"""人民银行爬虫"""

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from services.sentiment.base_scraper import BaseScraper

logger = logging.getLogger(__name__)


class PbcScraper(BaseScraper):
    source = "pbc"
    source_name = "人民银行"
    base_url = "https://www.pbc.gov.cn"
    tier = 3
    list_urls = [
        "https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html",
        "https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/125475/index.html",
    ]
    encoding = "utf-8"

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        articles = []

        # PBC 结构: font.newslist_style > a (新闻列表)
        # 也匹配通用 li/tr 内的链接
        for item in soup.select(
            "font.newslist_style, .newslist_style, "
            ".portlet_list li, .zhengce_list li"
        ):
            link = item.find("a")
            if not link or not link.get("href"):
                logger.debug(f"parse_list_page: [{self.source}] 列表项无链接，跳过")
                continue

            title = self._clean_text(link.get_text())
            if not title or len(title) < 4:
                logger.debug(f"parse_list_page: [{self.source}] 标题为空或过短，跳过")
                continue

            href = self._normalize_url(link["href"], url)

            pub_date = self._extract_date(item, href)
            if not pub_date:
                logger.debug(f"parse_list_page: [{self.source}] 无法提取日期，跳过: {title[:30]}")
                continue

            articles.append({
                "title": title,
                "url": href,
                "publish_date": pub_date,
                "category": "货币政策",
            })

        return articles

    def _extract_date(self, item, href: str = "") -> str:
        # PBC URL 含日期: /2026021411031036490/index.html
        m = re.search(r"/(\d{4})(\d{2})(\d{2})\d+/", href)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        for span in item.find_all(["span", "em", "td"]):
            text = span.get_text(strip=True)
            m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
            if m:
                return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        text = item.get_text()
        m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        logger.debug(f"_extract_date: [{self.source}] 所有模式均未匹配到日期")
        return ""

    def parse_article_page(self, html: str, url: str) -> Optional[dict]:
        soup = BeautifulSoup(html, "lxml")
        content = self._extract_body_text(soup, [
            "#zoom", ".content", ".TRS_Editor", "#content",
        ])
        return {"content": content} if content else None
