"""
爬虫注册表

SCRAPER_REGISTRY: source_id -> scraper class
TIER_MAP: tier -> [source_id, ...]
"""

from services.sentiment.scrapers.gov_cn import GovCnScraper
from services.sentiment.scrapers.xinhua import XinhuaScraper
from services.sentiment.scrapers.people import PeopleScraper
from services.sentiment.scrapers.ndrc import NdrcScraper
from services.sentiment.scrapers.miit import MiitScraper
from services.sentiment.scrapers.mofcom import MofcomScraper
from services.sentiment.scrapers.csrc import CsrcScraper
from services.sentiment.scrapers.pbc import PbcScraper
from services.sentiment.scrapers.nfra import NfraScraper
from services.sentiment.scrapers.nea import NeaScraper
from services.sentiment.scrapers.mohurd import MohurdScraper
from services.sentiment.scrapers.twitter_trump import TwitterTrumpScraper
from services.sentiment.scrapers.twitter_vance import TwitterVanceScraper
from services.sentiment.scrapers.twitter_rubio import TwitterRubioScraper
from services.sentiment.scrapers.cctv import CCTVScraper
from services.sentiment.scrapers.cninfo import CninfoScraper
from services.sentiment.scrapers.eastmoney import EastMoneyScraper
from services.sentiment.scrapers.cls import CLSScraper
from services.sentiment.scrapers.sina import SinaScraper
from services.sentiment.scrapers.polymarket import PolymarketScraper

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
