"""工信部爬虫

桌面版 miit.gov.cn 使用 JS 框架渲染列表，静态 HTML 无内容。
改用 wap.miit.gov.cn 主页（服务端渲染，含政策文件、新闻发布等约 60 篇文章）。
"""

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from sentiment.services.scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)


class MiitScraper(BaseScraper):
    source = "miit"
    source_name = "工信部"
    base_url = "https://wap.miit.gov.cn"
    tier = 2
    # wap 主页是服务端渲染，包含政策文件、新闻、司局动态等
    list_urls = [
        "https://wap.miit.gov.cn/",
    ]
    encoding = "utf-8"

    # 只关注政策相关的 URL 路径前缀
    _POLICY_PREFIXES = (
        "/zwgk/zcwj/",       # 政策文件
        "/zwgk/zcjd/",       # 政策解读
        "/zwgk/wjgs/",       # 文件公示
        "/xwfb/bldhd/",      # 部领导活动
        "/xwfb/gxdt/sjdt/",  # 司局动态
        "/xwfb/xwfbh/",      # 新闻发布会
        "/gzcy/yjzj/",       # 意见征集
    )

    # 分类映射
    _CATEGORY_MAP = {
        "/zwgk/zcwj/": "政策文件",
        "/zwgk/zcjd/": "政策解读",
        "/zwgk/wjgs/": "文件公示",
        "/xwfb/bldhd/": "部领导活动",
        "/xwfb/gxdt/sjdt/": "司局动态",
        "/xwfb/xwfbh/": "新闻发布会",
        "/gzcy/yjzj/": "意见征集",
    }

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        articles = []
        seen_urls: set[str] = set()

        # wap 主页文章在 <a> 标签中，href 含 /art/
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if "/art/" not in href:
                logger.debug(f"parse_list_page: [{self.source}] 非文章链接，跳过: {href[:50]}")
                continue

            # 只保留政策相关路径
            if not any(href.startswith(p) for p in self._POLICY_PREFIXES):
                logger.debug(f"parse_list_page: [{self.source}] 非政策相关路径，跳过: {href[:50]}")
                continue

            full_url = self._normalize_url(href)
            if full_url in seen_urls:
                logger.debug(f"parse_list_page: [{self.source}] URL 重复，跳过: {full_url}")
                continue
            seen_urls.add(full_url)

            title = self._clean_text(link.get_text())
            if not title or len(title) < 6:
                logger.debug(f"parse_list_page: [{self.source}] 标题为空或过短，跳过")
                continue

            pub_date = self._extract_date_from_context(link, href)
            if not pub_date:
                logger.debug(f"parse_list_page: [{self.source}] 无法提取日期，跳过: {title[:30]}")
                continue

            category = "工信部"
            for prefix, cat in self._CATEGORY_MAP.items():
                if href.startswith(prefix):
                    category = cat
                    logger.debug(f"parse_list_page: [{self.source}] 匹配分类 '{cat}'")
                    break

            articles.append({
                "title": title,
                "url": full_url,
                "publish_date": pub_date,
                "category": category,
            })

        return articles

    def _extract_date_from_context(self, link_tag, href: str) -> str:
        """从链接的兄弟元素或 URL 中提取日期。"""
        # 1. 兄弟 span 中找日期
        parent = link_tag.parent
        if parent:
            for span in parent.find_all("span"):
                text = span.get_text(strip=True)
                m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
                if m:
                    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        # 2. URL 中提取年份 /art/2026/
        m = re.search(r"/art/(\d{4})/", href)
        if m:
            return f"{m.group(1)}-01-01"

        logger.debug(f"_extract_date_from_context: [{self.source}] 所有模式均未匹配到日期")
        return ""

    def parse_article_page(self, html: str, url: str) -> Optional[dict]:
        soup = BeautifulSoup(html, "lxml")
        content = self._extract_body_text(soup, [
            ".article-content", ".xxgk_con", ".TRS_Editor",
            "#con_con", ".content",
        ])
        return {"content": content} if content else None
