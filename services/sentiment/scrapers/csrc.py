"""证监会爬虫

CSRC 官网改版后列表页使用 JS 渲染，静态 HTML 仅含旧数据。
改为抓取门户页，解析所有指向 /csrc/cNNN/cNNN/content.shtml 的链接。
"""

import logging
import re
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup

from services.sentiment.base_scraper import BaseScraper

logger = logging.getLogger(__name__)


class CsrcScraper(BaseScraper):
    source = "csrc"
    source_name = "证监会"
    base_url = "https://www.csrc.gov.cn"
    tier = 3
    list_urls = [
        # 门户页（服务端渲染，含各栏目最新文章）
        "https://www.csrc.gov.cn/csrc/c105930/common_list.shtml",
    ]
    encoding = "utf-8"

    # 文章详情 URL 模式
    _ARTICLE_RE = re.compile(r"/csrc/c\d+/c\d+/content\.shtml")

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        articles = []
        seen_hrefs: set[str] = set()

        for link in soup.find_all("a", href=self._ARTICLE_RE):
            href = self._normalize_url(link["href"], url)
            if href in seen_hrefs:
                logger.debug(f"parse_list_page: [{self.source}] URL 重复，跳过: {href}")
                continue

            title = self._clean_text(link.get_text())
            if not title or len(title) < 6:
                logger.debug(f"parse_list_page: [{self.source}] 标题为空或过短，跳过")
                continue

            seen_hrefs.add(href)

            # 从父级 <li> 提取日期
            li = link.find_parent("li")
            pub_date = self._extract_date(li or link.parent, href)
            if not pub_date:
                logger.debug(f"parse_list_page: [{self.source}] 无法提取日期，跳过: {title[:30]}")
                continue

            articles.append({
                "title": title,
                "url": href,
                "publish_date": pub_date,
                "category": "监管动态",
            })

        return articles

    def _extract_date(self, item, href: str = "") -> str:
        if item is None:
            logger.debug(f"_extract_date: [{self.source}] item 为 None，无法提取日期")
            return ""

        text = item.get_text(strip=True)

        # 完整日期: YYYY-MM-DD
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        # 短日期: MM-DD（门户页常见），推断为当前年份
        m = re.search(r"(\d{2})-(\d{2})$", text.rstrip())
        if m:
            year = datetime.now().year
            month, day = int(m.group(1)), int(m.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                return f"{year}-{month:02d}-{day:02d}"

        # URL 日期模式: /t20240101_xxx.shtml
        m = re.search(r"/t(\d{4})(\d{2})(\d{2})_", href)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        logger.debug(f"_extract_date: [{self.source}] 所有模式均未匹配到日期")
        return ""

    def parse_article_page(self, html: str, url: str) -> Optional[dict]:
        soup = BeautifulSoup(html, "lxml")
        content = self._extract_body_text(soup, [
            "#ContentRegion", ".article_content", ".TRS_Editor", "#content",
        ])
        return {"content": content} if content else None
