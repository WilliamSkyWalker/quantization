"""
爬虫注册表

SCRAPER_REGISTRY: source_id -> scraper class
TIER_MAP: tier -> [source_id, ...]
"""

from sentiment.scrapers.gov_cn import GovCnScraper
from sentiment.scrapers.xinhua import XinhuaScraper
from sentiment.scrapers.people import PeopleScraper
from sentiment.scrapers.ndrc import NdrcScraper
from sentiment.scrapers.miit import MiitScraper
from sentiment.scrapers.mofcom import MofcomScraper
from sentiment.scrapers.csrc import CsrcScraper
from sentiment.scrapers.pbc import PbcScraper
from sentiment.scrapers.nfra import NfraScraper
from sentiment.scrapers.nea import NeaScraper
from sentiment.scrapers.mohurd import MohurdScraper

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
}

TIER_MAP = {
    1: ["gov_cn", "xinhua", "people"],         # 最高层
    2: ["ndrc", "miit", "mofcom"],             # 产业层
    3: ["csrc", "pbc", "nfra"],                # 金融监管
    4: ["nea", "mohurd"],                      # 专项行业
}
