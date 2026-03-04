"""
爬虫注册表

SCRAPER_REGISTRY: source_id -> scraper class
TIER_MAP: tier -> [source_id, ...]
"""

from backend.services.sentiment.scrapers.gov_cn import GovCnScraper
from backend.services.sentiment.scrapers.xinhua import XinhuaScraper
from backend.services.sentiment.scrapers.people import PeopleScraper
from backend.services.sentiment.scrapers.ndrc import NdrcScraper
from backend.services.sentiment.scrapers.miit import MiitScraper
from backend.services.sentiment.scrapers.mofcom import MofcomScraper
from backend.services.sentiment.scrapers.csrc import CsrcScraper
from backend.services.sentiment.scrapers.pbc import PbcScraper
from backend.services.sentiment.scrapers.nfra import NfraScraper
from backend.services.sentiment.scrapers.nea import NeaScraper
from backend.services.sentiment.scrapers.mohurd import MohurdScraper
from backend.services.sentiment.scrapers.twitter_trump import TwitterTrumpScraper
from backend.services.sentiment.scrapers.twitter_vance import TwitterVanceScraper
from backend.services.sentiment.scrapers.twitter_rubio import TwitterRubioScraper
from backend.services.sentiment.scrapers.cctv import CCTVScraper
from backend.services.sentiment.scrapers.cninfo import CninfoScraper
from backend.services.sentiment.scrapers.eastmoney import EastMoneyScraper
from backend.services.sentiment.scrapers.cls import CLSScraper
from backend.services.sentiment.scrapers.sina import SinaScraper
from backend.services.sentiment.scrapers.polymarket import PolymarketScraper

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
