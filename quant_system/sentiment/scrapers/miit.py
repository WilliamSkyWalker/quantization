"""工信部爬虫

工信部网站使用 JS 框架渲染列表，静态 HTML 无文章内容。
本爬虫尝试从通用搜索结果页或RSS获取数据。
如果无法获取，返回空列表（不影响其他来源）。
"""

import re

from bs4 import BeautifulSoup

from sentiment.base_scraper import BaseScraper


class MiitScraper(BaseScraper):
    source = "miit"
    source_name = "工信部"
    base_url = "https://www.miit.gov.cn"
    tier = 2
    # 工信部列表页为 JS 渲染，尝试获取其公开搜索接口
    list_urls = [
        "https://www.miit.gov.cn/search/index.html?q=%E6%94%BF%E7%AD%96",
        "https://www.miit.gov.cn/jgsj/zbys/wjfb/index.html",
    ]
    encoding = "utf-8"

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        articles = []

        # 尝试多种选择器匹配工信部不同页面
        for item in soup.select(
            "ul.list li, .xxgk_list li, .gzdt-list li, "
            ".search-result-item, .result-item, "
            ".list li, li"
        ):
            link = item.find("a")
            if not link or not link.get("href"):
                continue

            title = self._clean_text(link.get_text())
            if not title or len(title) < 6:
                continue

            href = self._normalize_url(link["href"], url)
            if "miit.gov.cn" not in href and not href.startswith("/"):
                continue

            pub_date = self._extract_date(item, href)
            if not pub_date:
                continue

            articles.append({
                "title": title,
                "url": href,
                "publish_date": pub_date,
                "category": "政策文件",
            })

        return articles

    def _extract_date(self, item, href: str = "") -> str:
        for span in item.find_all(["span", "em"]):
            text = span.get_text(strip=True)
            m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
            if m:
                return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        # URL 中的日期
        m = re.search(r"/t(\d{4})(\d{2})(\d{2})_", href)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m = re.search(r"/art/(\d{4})/(\d{2})/", href)
        if m:
            return f"{m.group(1)}-{m.group(2)}-01"

        text = item.get_text()
        m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        return ""
