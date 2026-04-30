"""Vance Twitter/X 爬虫"""

from sentiment.services.scrapers.scrapers.twitter_base import TwitterBaseScraper


class TwitterVanceScraper(TwitterBaseScraper):
    source = "twitter_vance"
    source_name = "Twitter - Vance"
    username = "JDVance"
    user_id = "1326229737551912960"
    category = "US Policy - Vice President"
