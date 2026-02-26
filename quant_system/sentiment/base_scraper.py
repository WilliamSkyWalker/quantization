"""
爬虫抽象基类 + HTTP 限速器

- HttpRateLimiter: 滑动窗口 per-domain 限速
- BaseScraper: 子类只需实现 parse_list_page / parse_article_page
"""

import collections
import hashlib
import logging
import time
import threading
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from config.settings import (
    SENTIMENT_RATE_LIMIT,
    SENTIMENT_REQUEST_TIMEOUT,
    SENTIMENT_MAX_RETRIES,
    SENTIMENT_RETRY_WAIT,
    SENTIMENT_MAX_PAGES,
    SENTIMENT_USER_AGENT,
    LOG_LEVEL,
)

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


# ============================================================
# HTTP 限速器（per-domain 滑动窗口）
# ============================================================

class HttpRateLimiter:
    """
    Per-domain 滑动窗口限速器（线程安全）。

    在 60 秒窗口内每个域名最多 max_per_min 次请求。
    """

    def __init__(self, max_per_min: int = SENTIMENT_RATE_LIMIT):
        self.max_per_min = max_per_min
        self._domain_timestamps: dict[str, collections.deque] = {}
        self._lock = threading.Lock()

    def acquire(self, domain: str):
        """在发起请求前调用，按域名限速。"""
        with self._lock:
            if domain not in self._domain_timestamps:
                self._domain_timestamps[domain] = collections.deque()

            timestamps = self._domain_timestamps[domain]
            now = time.monotonic()

            # 清除 60 秒之前的记录
            while timestamps and now - timestamps[0] >= 60.0:
                timestamps.popleft()

            if len(timestamps) >= self.max_per_min:
                wait = 60.0 - (now - timestamps[0]) + 0.1
                if wait > 0:
                    logger.debug(f"[{domain}] 限速等待 {wait:.1f}s")
                    time.sleep(wait)
                now = time.monotonic()
                while timestamps and now - timestamps[0] >= 60.0:
                    timestamps.popleft()

            timestamps.append(time.monotonic())


# 全局限速器（所有爬虫共享）
_global_limiter = HttpRateLimiter()


# ============================================================
# 爬虫抽象基类
# ============================================================

class BaseScraper(ABC):
    """
    爬虫抽象基类。

    子类必须设置属性:
        source:      来源标识 (如 "gov_cn")
        source_name: 来源中文名 (如 "中国政府网")
        base_url:    基础 URL (如 "https://www.gov.cn")
        tier:        层级 (1~4)
        list_urls:   列表页 URL 模板列表
        encoding:    页面编码 (默认 "utf-8")

    子类必须实现:
        parse_list_page(html, url) -> list[dict]
    可选实现:
        parse_article_page(html, url) -> dict | None
    """

    source: str = ""
    source_name: str = ""
    base_url: str = ""
    tier: int = 1
    list_urls: list[str] = []
    encoding: str = "utf-8"

    def __init__(self, limiter: Optional[HttpRateLimiter] = None):
        self.limiter = limiter or _global_limiter
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": SENTIMENT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })

    @abstractmethod
    def parse_list_page(self, html: str, url: str) -> list[dict]:
        """
        解析列表页 HTML，返回文章元数据列表。

        每条记录应包含:
            - title: str  文章标题
            - url: str    文章 URL（绝对路径）
            - publish_date: str  发布日期 (YYYY-MM-DD)
            - category: str (可选) 栏目分类
            - summary: str (可选) 摘要
        """
        ...

    def parse_article_page(self, html: str, url: str) -> Optional[dict]:
        """
        解析文章详情页（可选）。

        Returns:
            补充信息字典（如 summary），或 None。
        """
        return None

    # ----------------------------------------------------------
    # HTTP 请求
    # ----------------------------------------------------------

    def fetch_page(self, url: str) -> Optional[str]:
        """
        HTTP GET 抓取页面，含限速 + 重试。

        Returns:
            页面 HTML 字符串，失败返回 None。
        """
        domain = urlparse(url).netloc
        for attempt in range(1, SENTIMENT_MAX_RETRIES + 1):
            self.limiter.acquire(domain)
            try:
                resp = self.session.get(
                    url,
                    timeout=SENTIMENT_REQUEST_TIMEOUT,
                    allow_redirects=True,
                )

                # 4xx 客户端错误不重试（403/404 等）
                if 400 <= resp.status_code < 500:
                    logger.warning(
                        f"[{self.source}] {url} 返回 {resp.status_code}，跳过"
                    )
                    return None

                resp.raise_for_status()

                # 处理编码
                if self.encoding:
                    resp.encoding = self.encoding
                elif resp.apparent_encoding:
                    resp.encoding = resp.apparent_encoding

                return resp.text
            except requests.RequestException as e:
                if attempt < SENTIMENT_MAX_RETRIES:
                    logger.warning(
                        f"[{self.source}] {url} 请求失败 (第{attempt}次): {e}"
                    )
                    time.sleep(SENTIMENT_RETRY_WAIT)
                else:
                    logger.error(
                        f"[{self.source}] {url} 重试 {SENTIMENT_MAX_RETRIES} 次后仍失败: {e}"
                    )
                    return None
        return None

    # ----------------------------------------------------------
    # 主抓取流程
    # ----------------------------------------------------------

    def scrape(self, max_pages: int = SENTIMENT_MAX_PAGES) -> list[dict]:
        """
        主抓取入口：遍历 list_urls → 解析 → 补充字段 → 去重。

        Args:
            max_pages: 每个列表 URL 最大翻页数。

        Returns:
            文章字典列表（已填充 source, tier, content_hash）。
        """
        all_articles = []
        seen_urls = set()

        for list_url_template in self.list_urls[:max_pages]:
            html = self.fetch_page(list_url_template)
            if not html:
                continue

            try:
                articles = self.parse_list_page(html, list_url_template)
            except Exception as e:
                logger.error(f"[{self.source}] 解析列表页失败 {list_url_template}: {e}")
                continue

            for article in articles:
                url = article.get("url", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                # 补充公共字段
                article["source"] = self.source
                article["tier"] = self.tier
                article["scraped_at"] = datetime.now()

                # 计算 content_hash
                title = article.get("title", "")
                pub_date = article.get("publish_date", "")
                article["content_hash"] = self._compute_content_hash(title, pub_date)

                all_articles.append(article)

        logger.info(f"[{self.source}] {self.source_name} 抓取完成: {len(all_articles)} 篇")
        return all_articles

    # ----------------------------------------------------------
    # 工具方法
    # ----------------------------------------------------------

    @staticmethod
    def _compute_content_hash(title: str, date: str) -> str:
        """SHA256(title+date) 用于跨源去重。"""
        raw = f"{title}|{date}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _normalize_url(self, href: str, base_url: str = "") -> str:
        """相对路径转绝对路径。"""
        if not href:
            return ""
        if href.startswith("http"):
            return href
        base = base_url or self.base_url
        return urljoin(base, href)

    @staticmethod
    def _clean_text(text: str, max_len: int = 200) -> str:
        """清理文本：去除多余空白，截断到指定长度。"""
        if not text:
            return ""
        text = " ".join(text.split())
        return text[:max_len]
