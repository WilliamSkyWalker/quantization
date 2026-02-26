"""Rubio Twitter/X 爬虫"""

from sentiment.scrapers.twitter_base import TwitterBaseScraper


class TwitterRubioScraper(TwitterBaseScraper):
    source = "twitter_rubio"
    source_name = "Twitter - Rubio"
    username = "marcorubio"
    user_id = "43201586"
    category = "US Policy - Secretary of State"
