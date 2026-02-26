"""Trump Twitter/X 爬虫"""

from sentiment.scrapers.twitter_base import TwitterBaseScraper


class TwitterTrumpScraper(TwitterBaseScraper):
    source = "twitter_trump"
    source_name = "Twitter - Trump"
    username = "realDonaldTrump"
    user_id = "25073877"
    category = "US Policy - President"
