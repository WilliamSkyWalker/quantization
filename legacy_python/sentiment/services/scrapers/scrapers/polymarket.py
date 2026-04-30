"""
Polymarket 预测市场爬虫（桥接已有 polymarket_alert → 舆情管道）

从 polymarket_alert 表读取有 LLM 分析结果的 alert，
转换为 policy_article + policy_analysis 格式，
让现有舆情因子（POLICY_SENT / POLICY_INTENSITY）自动吸收 Polymarket 信号。

不做实际 HTTP 抓取，不走 analyzer 重复分析。
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta

from services.config import LOG_LEVEL, SENTIMENT_MAX_PAGES
from sentiment.services.scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# alert category → impact_type 映射
_CATEGORY_IMPACT_MAP = {
    "politics": "general_policy",
    "economics": "monetary",
    "finance": "monetary",
    "crypto": "general_policy",
    "science": "general_policy",
    "sports": "general_policy",
    "culture": "general_policy",
    "climate": "general_policy",
    "geopolitics": "general_policy",
    "trade": "trade_tariff",
    "technology": "tech_sanction",
}


class PolymarketScraper(BaseScraper):
    """
    Polymarket 预测市场桥接爬虫。

    从 polymarket_alert 表读取有 LLM 分析的 alert，
    转为 policy_article 格式。同时附带 _analysis 元数据，
    供 downloader 一并注入 policy_analysis。

    max_pages 复用为回看天数。
    """

    source = "polymarket"
    source_name = "Polymarket预测市场"
    base_url = "https://polymarket.com"
    tier = 8
    list_urls = []
    fetch_content = False

    def parse_list_page(self, html: str, url: str) -> list[dict]:
        """Polymarket 不使用 HTML 列表页。"""
        return []

    def scrape_pages(self, max_pages: int = SENTIMENT_MAX_PAGES, **kwargs):
        """
        从 polymarket_alert 表读取 alert，按天分批 yield。

        Args:
            max_pages: 回看天数。

        Yields:
            list[dict] — 当天的文章列表（附带 _analysis 元数据）。
        """
        # DatabaseManager 已废弃
        from sentiment.services.polymarket.models import PolymarketAlert, PolymarketEvent

        db = None  # DatabaseManager 已废弃
        session = db.get_session()

        try:
            cutoff = datetime.now() - timedelta(days=max_pages)

            # 查询有 LLM 分析结果的 alert
            alerts = (
                session.query(PolymarketAlert)
                .filter(
                    PolymarketAlert.created_at >= cutoff,
                    PolymarketAlert.llm_summary.isnot(None),
                    PolymarketAlert.llm_sentiment.isnot(None),
                )
                .order_by(PolymarketAlert.created_at.desc())
                .all()
            )

            if not alerts:
                logger.info(f"[{self.source}] 无可桥接的 Polymarket alert")
                return

            # 预加载 event slug 映射
            condition_ids = list({a.condition_id for a in alerts})
            events = (
                session.query(PolymarketEvent)
                .filter(PolymarketEvent.condition_id.in_(condition_ids))
                .all()
            )
            slug_map = {e.condition_id: e.slug for e in events if e.slug}

            # 按日期分组
            day_groups: dict[str, list] = {}
            for alert in alerts:
                day_key = alert.created_at.strftime("%Y-%m-%d") if alert.created_at else "unknown"
                day_groups.setdefault(day_key, []).append(alert)

            seen_hashes: set[str] = set()

            for day_key in sorted(day_groups.keys(), reverse=True):
                day_alerts = day_groups[day_key]
                day_articles = []

                for alert in day_alerts:
                    article, analysis = self._alert_to_article(alert, slug_map, day_key)

                    if article["content_hash"] in seen_hashes:
                        logger.debug(f"scrape_pages: [{self.source}] 重复内容哈希，跳过 alert")
                        continue
                    seen_hashes.add(article["content_hash"])

                    # 附带 analysis 元数据
                    article["_analysis"] = analysis
                    day_articles.append(article)

                if day_articles:
                    yield day_articles

            logger.info(f"[{self.source}] 桥接完成: {len(alerts)} 个 alert")

        finally:
            session.close()

    def _alert_to_article(
        self,
        alert,
        slug_map: dict[str, str],
        day_key: str,
    ) -> tuple[dict, dict]:
        """
        将 PolymarketAlert 转为 (policy_article dict, policy_analysis dict)。
        """
        # 构建标题
        direction = "↑" if (alert.price_change or 0) > 0 else "↓"
        change_pct = abs(alert.price_change or 0) * 100
        title = f"[Polymarket] {alert.question or 'Unknown'} ({direction}{change_pct:.1f}%)"
        title = title[:500]

        # 构建 URL
        slug = slug_map.get(alert.condition_id, "")
        if slug:
            url = f"https://polymarket.com/event/{slug}"
        else:
            url = f"https://polymarket.com/event/{alert.condition_id}"

        # 确保 URL 唯一（追加 alert 类型和时间戳）
        ts_str = alert.created_at.strftime("%Y%m%d%H%M%S") if alert.created_at else "0"
        url = f"{url}#alert-{alert.alert_type}-{ts_str}"

        content = alert.llm_summary or ""
        publish_date = day_key

        content_hash = self._compute_content_hash(title, publish_date)

        # 解析 industries
        sw_industries = self._parse_json_field(alert.affected_sw_industries)
        industries_str = ",".join(
            item if isinstance(item, str) else str(item)
            for item in sw_industries
        ) if sw_industries else ""

        # 解析 affected_stocks
        a_shares = self._parse_json_field(alert.affected_a_shares)
        affected_stocks_json = json.dumps(a_shares, ensure_ascii=False) if a_shares else ""

        # impact_type
        category = (alert.question or "").split()[0].lower() if alert.question else ""
        # 尝试从 alert 的 polymarket_event category 推断
        impact_type = _CATEGORY_IMPACT_MAP.get(category, "general_policy")

        article = {
            "source": self.source,
            "tier": self.tier,
            "title": title,
            "url": url,
            "publish_date": publish_date,
            "category": "Polymarket",
            "summary": content[:2000] if content else title,
            "content": content,
            "content_hash": content_hash,
            "scraped_at": datetime.now(),
        }

        analysis = {
            "analysis_type": "llm",
            "industries": industries_str,
            "sentiment": alert.llm_sentiment,
            "intensity": alert.llm_confidence or 0.5,
            "impact_type": impact_type,
            "keywords_hit": "",
            "summary_text": content[:2000] if content else "",
            "affected_stocks": affected_stocks_json,
            "analyzed_at": datetime.now(),
        }

        return article, analysis

    @staticmethod
    def _parse_json_field(value) -> list:
        """解析可能是 JSON 字符串或已解析的 list 的字段。"""
        if not value:
            logger.debug("_parse_json_field: 值为空，返回空列表")
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, TypeError):
                logger.debug(f"_parse_json_field: JSON 解析失败: {str(value)[:50]}")
                return []
        logger.debug(f"_parse_json_field: 不支持的类型 {type(value)}，返回空列表")
        return []

    def scrape(self, max_pages: int = SENTIMENT_MAX_PAGES) -> list[dict]:
        """抓取入口。"""
        all_articles = []
        for day_articles in self.scrape_pages(max_pages):
            all_articles.extend(day_articles)
        logger.info(f"[{self.source}] {self.source_name} 桥接完成: {len(all_articles)} 篇")
        return all_articles
