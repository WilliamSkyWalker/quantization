"""Twitter/X 爬虫单元测试（twikit 版本）。"""

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest


# ============================================================
# Helpers: mock twikit Tweet / Result
# ============================================================

def _make_mock_tweet(tweet_id="123456", text="Test tweet",
                     created_at="Wed Jan 15 12:30:00 +0000 2025",
                     retweet_count=10, favorite_count=100):
    """构造 mock twikit Tweet 对象。"""
    tweet = MagicMock()
    tweet.id = tweet_id
    tweet.text = text
    tweet.created_at = created_at
    tweet.retweet_count = retweet_count
    tweet.favorite_count = favorite_count
    return tweet


def _make_mock_result(tweets, has_next=False, next_tweets=None):
    """构造 mock twikit Result 对象（可迭代 + async .next()）。"""
    result = MagicMock()
    result.__iter__ = MagicMock(return_value=iter(tweets))

    if has_next and next_tweets is not None:
        next_result = MagicMock()
        next_result.__iter__ = MagicMock(return_value=iter(next_tweets))
        next_result.next = AsyncMock(return_value=None)
        result.next = AsyncMock(return_value=next_result)
    else:
        result.next = AsyncMock(return_value=None)

    return result


# ============================================================
# 测试 _tweet_to_article 字段映射
# ============================================================

class TestTweetToArticle:
    """测试 _tweet_to_article 正确映射字段。"""

    def test_valid_tweet_mapping(self):
        """有效推文映射到正确的 PolicyArticle 字段。"""
        from sentiment.scrapers.twitter_trump import TwitterTrumpScraper
        scraper = TwitterTrumpScraper()

        tweet = _make_mock_tweet(
            tweet_id="789",
            text="Big trade deal coming!",
            created_at="Mon Mar 10 08:00:00 +0000 2025",
            retweet_count=5000,
            favorite_count=20000,
        )

        article = scraper._tweet_to_article(tweet)
        assert article["source"] == "twitter_trump"
        assert article["tier"] == 5
        assert article["title"] == "Big trade deal coming!"
        assert article["url"] == "https://x.com/realDonaldTrump/status/789"
        assert article["publish_date"] == "2025-03-10"
        assert article["category"] == "US Policy - President"
        assert "RT:5000" in article["summary"]
        assert "like:20000" in article["summary"]
        assert article["content_hash"]  # 非空

    def test_title_truncated_to_500(self):
        """推文文本超过 500 字符时 title 截断。"""
        from sentiment.scrapers.twitter_trump import TwitterTrumpScraper
        scraper = TwitterTrumpScraper()

        long_text = "A" * 600
        tweet = _make_mock_tweet(text=long_text)
        article = scraper._tweet_to_article(tweet)

        assert len(article["title"]) == 500
        assert long_text in article["summary"]  # summary 保留全文

    def test_vance_scraper_mapping(self):
        """Vance 子类字段正确。"""
        from sentiment.scrapers.twitter_vance import TwitterVanceScraper
        scraper = TwitterVanceScraper()

        tweet = _make_mock_tweet(tweet_id="1", text="VP tweet")
        article = scraper._tweet_to_article(tweet)

        assert article["source"] == "twitter_vance"
        assert article["category"] == "US Policy - Vice President"
        assert article["url"] == "https://x.com/JDVance/status/1"

    def test_missing_date(self):
        """created_at 为空时 publish_date 为空字符串。"""
        from sentiment.scrapers.twitter_trump import TwitterTrumpScraper
        scraper = TwitterTrumpScraper()

        tweet = _make_mock_tweet(created_at=None)
        article = scraper._tweet_to_article(tweet)
        assert article["publish_date"] == ""

    def test_zero_metrics(self):
        """互动数据为 None 时默认 0。"""
        from sentiment.scrapers.twitter_trump import TwitterTrumpScraper
        scraper = TwitterTrumpScraper()

        tweet = _make_mock_tweet(retweet_count=None, favorite_count=None)
        article = scraper._tweet_to_article(tweet)
        assert "RT:0" in article["summary"]
        assert "like:0" in article["summary"]


# ============================================================
# 凭证缺失时优雅降级
# ============================================================

class TestMissingCredentials:
    """凭证为空时 scrape() 返回空列表。"""

    @patch("sentiment.scrapers.twitter_base.TWITTER_USERNAME", "")
    @patch("sentiment.scrapers.twitter_base.TWITTER_EMAIL", "")
    @patch("sentiment.scrapers.twitter_base.TWITTER_PASSWORD", "")
    def test_scrape_returns_empty_without_credentials(self):
        """无凭证时直接返回空列表。"""
        from sentiment.scrapers.twitter_trump import TwitterTrumpScraper
        scraper = TwitterTrumpScraper()

        result = scraper.scrape()
        assert result == []

    @patch("sentiment.scrapers.twitter_base.Client", None)
    def test_scrape_returns_empty_without_twikit(self):
        """twikit 未安装时直接返回空列表。"""
        from sentiment.scrapers.twitter_trump import TwitterTrumpScraper
        scraper = TwitterTrumpScraper()

        result = scraper.scrape()
        assert result == []


# ============================================================
# 转推过滤
# ============================================================

class TestRetweetFiltering:
    """以 "RT @" 开头的推文被过滤。"""

    def test_is_retweet_true(self):
        from sentiment.scrapers.twitter_trump import TwitterTrumpScraper
        scraper = TwitterTrumpScraper()

        tweet = _make_mock_tweet(text="RT @someone: some retweet content")
        assert scraper._is_retweet(tweet) is True

    def test_is_retweet_false(self):
        from sentiment.scrapers.twitter_trump import TwitterTrumpScraper
        scraper = TwitterTrumpScraper()

        tweet = _make_mock_tweet(text="Original tweet mentioning RT numbers")
        assert scraper._is_retweet(tweet) is False

    @patch("sentiment.scrapers.twitter_base.TWITTER_USERNAME", "user")
    @patch("sentiment.scrapers.twitter_base.TWITTER_EMAIL", "e@e.com")
    @patch("sentiment.scrapers.twitter_base.TWITTER_PASSWORD", "pass")
    @patch("sentiment.scrapers.twitter_base.Client")
    def test_retweets_excluded_from_scrape(self, MockClient):
        """scrape() 结果中不包含转推。"""
        from sentiment.scrapers.twitter_trump import TwitterTrumpScraper
        scraper = TwitterTrumpScraper()

        original = _make_mock_tweet(tweet_id="1", text="Original")
        retweet = _make_mock_tweet(tweet_id="2", text="RT @someone: retweet")
        result = _make_mock_result([original, retweet])

        mock_client = AsyncMock()
        mock_client.load_cookies = MagicMock()
        mock_client.get_user_tweets = AsyncMock(return_value=result)
        MockClient.return_value = mock_client

        articles = scraper.scrape(max_pages=1)
        assert len(articles) == 1
        assert articles[0]["title"] == "Original"


# ============================================================
# 分页
# ============================================================

class TestPagination:
    """分页逻辑。"""

    @patch("sentiment.scrapers.twitter_base.TWITTER_USERNAME", "user")
    @patch("sentiment.scrapers.twitter_base.TWITTER_EMAIL", "e@e.com")
    @patch("sentiment.scrapers.twitter_base.TWITTER_PASSWORD", "pass")
    @patch("sentiment.scrapers.twitter_base.Client")
    def test_two_pages_merged(self, MockClient):
        """两页数据正确合并。"""
        from sentiment.scrapers.twitter_trump import TwitterTrumpScraper
        scraper = TwitterTrumpScraper()

        page1_tweets = [_make_mock_tweet(tweet_id="1", text="Page 1 tweet")]
        page2_tweets = [_make_mock_tweet(tweet_id="2", text="Page 2 tweet")]
        result = _make_mock_result(page1_tweets, has_next=True, next_tweets=page2_tweets)

        mock_client = AsyncMock()
        mock_client.load_cookies = MagicMock()
        mock_client.get_user_tweets = AsyncMock(return_value=result)
        MockClient.return_value = mock_client

        articles = scraper.scrape(max_pages=5)
        assert len(articles) == 2
        urls = {a["url"] for a in articles}
        assert "https://x.com/realDonaldTrump/status/1" in urls
        assert "https://x.com/realDonaldTrump/status/2" in urls

    @patch("sentiment.scrapers.twitter_base.TWITTER_USERNAME", "user")
    @patch("sentiment.scrapers.twitter_base.TWITTER_EMAIL", "e@e.com")
    @patch("sentiment.scrapers.twitter_base.TWITTER_PASSWORD", "pass")
    @patch("sentiment.scrapers.twitter_base.Client")
    def test_deduplicates_tweets(self, MockClient):
        """重复 tweet_id 被去重。"""
        from sentiment.scrapers.twitter_trump import TwitterTrumpScraper
        scraper = TwitterTrumpScraper()

        t1 = _make_mock_tweet(tweet_id="1", text="Same tweet")
        t2 = _make_mock_tweet(tweet_id="1", text="Same tweet")
        result = _make_mock_result([t1, t2])

        mock_client = AsyncMock()
        mock_client.load_cookies = MagicMock()
        mock_client.get_user_tweets = AsyncMock(return_value=result)
        MockClient.return_value = mock_client

        articles = scraper.scrape(max_pages=1)
        assert len(articles) == 1

    @patch("sentiment.scrapers.twitter_base.TWITTER_USERNAME", "user")
    @patch("sentiment.scrapers.twitter_base.TWITTER_EMAIL", "e@e.com")
    @patch("sentiment.scrapers.twitter_base.TWITTER_PASSWORD", "pass")
    @patch("sentiment.scrapers.twitter_base.Client")
    def test_fetch_failure_returns_empty(self, MockClient):
        """get_user_tweets 抛异常时返回空列表。"""
        from sentiment.scrapers.twitter_trump import TwitterTrumpScraper
        scraper = TwitterTrumpScraper()

        mock_client = AsyncMock()
        mock_client.load_cookies = MagicMock()
        mock_client.get_user_tweets = AsyncMock(side_effect=Exception("Network error"))
        MockClient.return_value = mock_client

        articles = scraper.scrape(max_pages=5)
        assert articles == []


# ============================================================
# 注册表完整性
# ============================================================

class TestRegistry:
    """爬虫注册表包含 Twitter 来源。"""

    def test_twitter_scrapers_registered(self):
        from sentiment.scrapers import SCRAPER_REGISTRY, TIER_MAP

        assert "twitter_trump" in SCRAPER_REGISTRY
        assert "twitter_vance" in SCRAPER_REGISTRY
        assert "twitter_rubio" in SCRAPER_REGISTRY

    def test_tier_5_exists(self):
        from sentiment.scrapers import TIER_MAP

        assert 5 in TIER_MAP
        assert set(TIER_MAP[5]) == {"twitter_trump", "twitter_vance", "twitter_rubio"}

    def test_total_scraper_count(self):
        from sentiment.scrapers import SCRAPER_REGISTRY

        assert len(SCRAPER_REGISTRY) == 14  # 11 gov + 3 twitter

    def test_all_tier_sources_in_registry(self):
        """TIER_MAP 中的所有 source 都在 SCRAPER_REGISTRY 中。"""
        from sentiment.scrapers import SCRAPER_REGISTRY, TIER_MAP

        for tier, sources in TIER_MAP.items():
            for src in sources:
                assert src in SCRAPER_REGISTRY, f"{src} (tier {tier}) not in registry"


# ============================================================
# 各子类属性正确性
# ============================================================

class TestSubclassAttributes:
    """各 Twitter 子类属性正确设置。"""

    def test_trump_scraper(self):
        from sentiment.scrapers.twitter_trump import TwitterTrumpScraper
        s = TwitterTrumpScraper()
        assert s.source == "twitter_trump"
        assert s.username == "realDonaldTrump"
        assert s.user_id == "25073877"
        assert s.tier == 5
        assert s.category == "US Policy - President"

    def test_vance_scraper(self):
        from sentiment.scrapers.twitter_vance import TwitterVanceScraper
        s = TwitterVanceScraper()
        assert s.source == "twitter_vance"
        assert s.username == "JDVance"
        assert s.user_id == "1326229737551912960"
        assert s.tier == 5
        assert s.category == "US Policy - Vice President"

    def test_rubio_scraper(self):
        from sentiment.scrapers.twitter_rubio import TwitterRubioScraper
        s = TwitterRubioScraper()
        assert s.source == "twitter_rubio"
        assert s.username == "marcorubio"
        assert s.user_id == "43201586"
        assert s.tier == 5
        assert s.category == "US Policy - Secretary of State"
