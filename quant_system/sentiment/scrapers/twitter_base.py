"""
Twitter/X 爬虫基类（twikit 免费方案）

通过 twikit 库使用 Twitter 内部 API 获取指定账号的原创推文。
只需普通 Twitter 账号，无需付费 API。
3 个 Twitter 爬虫共享独立的 HttpRateLimiter 实例（90 req/min）。
缺少登录凭证时优雅降级（跳过，不报错）。
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

try:
    from twikit import Client
except ImportError:
    Client = None

from config.settings import (
    LOG_LEVEL,
    TWITTER_USERNAME,
    TWITTER_EMAIL,
    TWITTER_PASSWORD,
    TWITTER_COOKIES_FILE,
    TWITTER_RATE_LIMIT,
    TWITTER_MAX_TWEETS,
)
from sentiment.base_scraper import BaseScraper, HttpRateLimiter

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# 3 个 Twitter 爬虫共享的独立限速器
_twitter_limiter = HttpRateLimiter(max_per_min=TWITTER_RATE_LIMIT)


class TwitterBaseScraper(BaseScraper):
    """
    Twitter/X 爬虫基类（twikit）。

    子类只需设置类属性:
        source:    来源标识，如 "twitter_trump"
        source_name: 中文名
        username:  Twitter 用户名（用于构建推文 URL）
        user_id:   Twitter 用户 ID（API 查询用）
        category:  分类标签，如 "US Policy - President"
    """

    tier: int = 5
    base_url: str = "https://x.com"
    list_urls: list[str] = []
    encoding: str = "utf-8"

    # 子类必须覆盖
    username: str = ""
    user_id: str = ""
    category: str = ""

    def __init__(self, limiter: Optional[HttpRateLimiter] = None):
        super().__init__(limiter=limiter or _twitter_limiter)

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        """Twitter 不使用 HTML 列表页，此方法不会被调用。"""
        return []

    def scrape(self, max_pages: int = 5) -> list[dict]:
        """
        抓取推文入口。

        凭证为空或 twikit 未安装时直接返回空列表并打印警告。
        """
        if Client is None:
            logger.warning(
                f"[{self.source}] twikit not installed, skipping Twitter scrape"
            )
            return []

        if not (TWITTER_USERNAME and TWITTER_EMAIL and TWITTER_PASSWORD):
            logger.warning(
                f"[{self.source}] Twitter credentials not set, skipping Twitter scrape"
            )
            return []

        return asyncio.run(self._async_scrape(max_pages))

    async def _async_scrape(self, max_pages: int) -> list[dict]:
        """异步抓取推文，使用 twikit Client。"""
        client = Client("en-US")

        # 尝试加载已保存的 cookies，避免重复登录
        try:
            client.load_cookies(TWITTER_COOKIES_FILE)
            logger.debug(f"[{self.source}] Loaded cookies from {TWITTER_COOKIES_FILE}")
        except Exception:
            logger.info(f"[{self.source}] No saved cookies, logging in...")
            try:
                await client.login(
                    auth_info_1=TWITTER_USERNAME,
                    auth_info_2=TWITTER_EMAIL,
                    password=TWITTER_PASSWORD,
                )
                client.save_cookies(TWITTER_COOKIES_FILE)
                logger.info(f"[{self.source}] Login successful, cookies saved")
            except Exception as e:
                logger.error(f"[{self.source}] Twitter login failed: {e}")
                return []

        all_articles = []
        seen_ids: set[str] = set()

        try:
            result = await client.get_user_tweets(
                self.user_id, "Tweets", count=TWITTER_MAX_TWEETS
            )
        except Exception as e:
            logger.error(f"[{self.source}] Failed to fetch tweets: {e}")
            return []

        for page_num in range(max_pages):
            if result is None:
                break

            for tweet in result:
                if self._is_retweet(tweet):
                    continue
                tweet_id = str(tweet.id)
                if tweet_id in seen_ids:
                    continue
                seen_ids.add(tweet_id)
                article = self._tweet_to_article(tweet)
                all_articles.append(article)

            # 下一页
            if page_num < max_pages - 1:
                try:
                    result = await result.next()
                except Exception:
                    break
            else:
                break

        logger.info(f"[{self.source}] {self.source_name} 抓取完成: {len(all_articles)} 篇")
        return all_articles

    def _tweet_to_article(self, tweet) -> dict:
        """将 twikit Tweet 对象映射到 PolicyArticle dict。"""
        tweet_id = str(tweet.id)
        text = tweet.text or ""
        tweet_url = f"https://x.com/{self.username}/status/{tweet_id}"

        # 解析日期: twikit 返回 "Wed Jan 15 12:30:00 +0000 2025"
        publish_date = ""
        if tweet.created_at:
            try:
                dt = datetime.strptime(tweet.created_at, "%a %b %d %H:%M:%S %z %Y")
                publish_date = dt.strftime("%Y-%m-%d")
            except ValueError:
                publish_date = ""

        title = text[:500]
        rt_count = tweet.retweet_count or 0
        like_count = tweet.favorite_count or 0
        summary = f"{text} [RT:{rt_count}, like:{like_count}]"

        return {
            "source": self.source,
            "tier": self.tier,
            "title": title,
            "url": tweet_url,
            "publish_date": publish_date,
            "category": self.category,
            "summary": summary,
            "content_hash": self._compute_content_hash(title, publish_date),
            "scraped_at": datetime.now(),
        }

    @staticmethod
    def _is_retweet(tweet) -> bool:
        """检查是否为转推（twikit 的 'Tweets' 类型不保证排除转推）。"""
        text = tweet.text or ""
        return text.startswith("RT @")
