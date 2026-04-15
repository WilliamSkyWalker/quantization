"""
美股 Polymarket 情感因子: POLYMARKET_SENT

从 polymarket_alert 表的 affected_tickers 提取个股级情感信号。
每条 alert 包含 LLM 分析的 affected_tickers JSON:
    [{"ticker": "XOM", "direction": "bearish", "confidence": 0.85}, ...]

因子值 = 过去 N 天内该 ticker 所有 alert 的加权情感得分（时间衰减）。

方向映射: bullish → +1, bearish → -1, neutral → 0
最终得分 = Σ(direction × confidence × llm_confidence × time_decay)
"""

import json
import logging
from typing import Optional

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_LOOKBACK_DAYS = 14       # 回看天数
_TIME_DECAY = 0.3         # 指数衰减系数（越大衰减越快）
_DIRECTION_MAP = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}


class PolymarketSent(USFactorBase):
    """Polymarket Sentiment: per-ticker sentiment from prediction market alerts"""
    name = "POLYMARKET_SENT"
    description = "Polymarket 预测市场情感因子"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = set(universe["ticker"].tolist())
        date_ts = pd.to_datetime(date)
        start_ts = date_ts - pd.Timedelta(days=_LOOKBACK_DAYS)

        # 查询 polymarket_alert
        alerts = self._get_alerts(start_ts, date_ts)
        if alerts.empty:
            logger.debug("PolymarketSent.compute: 无Polymarket alerts数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 解析 affected_tickers JSON，构建 per-ticker 信号
        records = []
        for _, row in alerts.iterrows():
            tickers_json = row.get("affected_tickers")
            if not tickers_json:
                logger.debug("PolymarketSent.compute: alert affected_tickers为空，跳过")
                continue
            try:
                affected = json.loads(tickers_json) if isinstance(tickers_json, str) else tickers_json
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"PolymarketSent.compute: JSON解析失败: {e}")
                continue

            if not isinstance(affected, list):
                logger.debug("PolymarketSent.compute: affected_tickers不是列表，跳过")
                continue

            llm_conf = float(row.get("llm_confidence") or 0)
            created = pd.to_datetime(row.get("created_at"))
            days_ago = (date_ts - created).total_seconds() / 86400
            if days_ago < 0:
                logger.debug("PolymarketSent.compute: alert日期在计算日之后，跳过")
                continue
            time_weight = np.exp(-_TIME_DECAY * days_ago)

            for item in affected:
                if not isinstance(item, dict):
                    logger.debug("PolymarketSent.compute: affected_tickers元素不是字典，跳过")
                    continue
                ticker = item.get("ticker", "")
                if ticker not in tickers:
                    logger.debug(f"PolymarketSent.compute: ticker {ticker} 不在universe中，跳过")
                    continue
                direction = _DIRECTION_MAP.get(item.get("direction", ""), 0.0)
                confidence = float(item.get("confidence", 0.5))

                score = direction * confidence * llm_conf * time_weight
                records.append({"ticker": ticker, "score": score})

        if not records:
            logger.debug("PolymarketSent.compute: 解析后无有效情感记录")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = pd.DataFrame(records)
        # 聚合：每只股票的所有 alert 信号求和
        result = df.groupby("ticker")["score"].sum().reset_index()
        result.columns = ["ticker", "factor_value"]
        return result

    def _get_alerts(self, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        """获取时间范围内的 polymarket alerts。"""
        cache_key = ("pm_alerts", start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            from sentiment.services.polymarket.models import PolymarketAlert
            from django.db.models import Q
            qs = PolymarketAlert.objects.filter(
                created_at__gte=start,
                created_at__lte=end,
            ).exclude(
                Q(affected_tickers__isnull=True) | Q(affected_tickers="")
            ).order_by("-created_at").values(
                "affected_tickers", "llm_confidence", "llm_sentiment", "created_at"
            )
            df = pd.DataFrame(qs)
        except Exception as e:
            logger.debug(f"PolymarketSent: 查询失败: {e}")
            df = pd.DataFrame()
        self._date_cache[cache_key] = df
        return df
