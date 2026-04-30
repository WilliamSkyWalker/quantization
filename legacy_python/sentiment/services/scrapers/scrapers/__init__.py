"""
爬虫注册表

SCRAPER_REGISTRY: source_id -> scraper class
TIER_MAP: tier -> [source_id, ...]
"""

from sentiment.services.scrapers.scrapers.gov_cn import GovCnScraper
from sentiment.services.scrapers.scrapers.xinhua import XinhuaScraper
from sentiment.services.scrapers.scrapers.people import PeopleScraper
from sentiment.services.scrapers.scrapers.ndrc import NdrcScraper
from sentiment.services.scrapers.scrapers.miit import MiitScraper
from sentiment.services.scrapers.scrapers.mofcom import MofcomScraper
from sentiment.services.scrapers.scrapers.csrc import CsrcScraper
from sentiment.services.scrapers.scrapers.pbc import PbcScraper
from sentiment.services.scrapers.scrapers.nfra import NfraScraper
from sentiment.services.scrapers.scrapers.nea import NeaScraper
from sentiment.services.scrapers.scrapers.mohurd import MohurdScraper
from sentiment.services.scrapers.scrapers.twitter_trump import TwitterTrumpScraper
from sentiment.services.scrapers.scrapers.twitter_vance import TwitterVanceScraper
from sentiment.services.scrapers.scrapers.twitter_rubio import TwitterRubioScraper
from sentiment.services.scrapers.scrapers.cctv import CCTVScraper
from sentiment.services.scrapers.scrapers.cninfo import CninfoScraper
from sentiment.services.scrapers.scrapers.eastmoney import EastMoneyScraper
from sentiment.services.scrapers.scrapers.cls import CLSScraper
from sentiment.services.scrapers.scrapers.sina import SinaScraper
from sentiment.services.scrapers.scrapers.polymarket import PolymarketScraper

SCRAPER_REGISTRY = {
    "gov_cn": GovCnScraper,
    "xinhua": XinhuaScraper,
    "people": PeopleScraper,
    "ndrc": NdrcScraper,
    "miit": MiitScraper,
    "mofcom": MofcomScraper,
    "csrc": CsrcScraper,
    "pbc": PbcScraper,
    "nfra": NfraScraper,
    "nea": NeaScraper,
    "mohurd": MohurdScraper,
    "twitter_trump": TwitterTrumpScraper,
    "twitter_vance": TwitterVanceScraper,
    "twitter_rubio": TwitterRubioScraper,
    "cctv": CCTVScraper,
    "cninfo": CninfoScraper,
    "eastmoney": EastMoneyScraper,
    "cls": CLSScraper,
    "sina": SinaScraper,
    "polymarket": PolymarketScraper,
}

TIER_MAP = {
    1: ["gov_cn", "xinhua", "people", "cctv"],   # 最高层
    2: ["ndrc", "miit", "mofcom", "cninfo"],      # 产业层
    3: ["csrc", "pbc", "nfra"],                # 金融监管
    4: ["nea", "mohurd"],                      # 专项行业
    5: ["twitter_trump", "twitter_vance", "twitter_rubio"],  # 美国政策
    6: ["eastmoney", "cls", "sina"],           # 财经媒体
    8: ["polymarket"],                        # 预测市场
}
