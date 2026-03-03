"""
Polymarket 事件驱动回测引擎

回放已结算市场的历史价格序列，模拟 Spike 检测 + LLM 分析，
评估事件驱动策略的历史表现。

与 A 股回测模块完全独立。
"""

import json
import logging
import time
from collections import deque
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from backend.services.config import (
    POLYMARKET_SPIKE_5M,
    POLYMARKET_SPIKE_1H,
    POLYMARKET_SPIKE_24H,
    POLYMARKET_LLM_COOLDOWN,
    LOG_LEVEL,
)
from backend.services.data.database import DatabaseManager
from backend.services.polymarket.models import PolymarketEvent, PolymarketPriceSnapshot, PolymarketAlert
from backend.services.polymarket.event_analyzer import EventAnalyzer
from backend.tasks.manager import task_manager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

SPIKE_RULES = [
    ("spike_5m", 300, POLYMARKET_SPIKE_5M),
    ("spike_1h", 3600, POLYMARKET_SPIKE_1H),
    ("spike_24h", 86400, POLYMARKET_SPIKE_24H),
]


class PolymarketBacktester:
    """
    Polymarket 事件驱动回测引擎。

    核心流程：
    1. 读取已结算市场的历史价格序列
    2. 按时间顺序回放价格
    3. 使用与实时监控相同的 Spike 检测逻辑
    4. 触发 LLM 分析（可选跳过以节省 API 调用）
    5. 生成回测报告
    """

    def __init__(self):
        self._db = DatabaseManager()
        self._analyzer = EventAnalyzer()

    def run(
        self,
        task_id: str,
        condition_ids: Optional[list[str]] = None,
        use_llm: bool = True,
        spike_5m: Optional[float] = None,
        spike_1h: Optional[float] = None,
        spike_24h: Optional[float] = None,
    ) -> dict:
        """
        运行回测。

        Args:
            task_id: 任务 ID
            condition_ids: 要回测的市场列表，None 则回测所有已结算市场
            use_llm: 是否调用 LLM 分析（False 则只做 Spike 检测）
            spike_5m/1h/24h: 自定义阈值，None 使用默认

        Returns:
            回测结果字典
        """
        self._db.init_tables()
        task_manager.update_progress(task_id, 5, "加载历史数据...")

        # 自定义阈值
        rules = [
            ("spike_5m", 300, spike_5m if spike_5m is not None else POLYMARKET_SPIKE_5M),
            ("spike_1h", 3600, spike_1h if spike_1h is not None else POLYMARKET_SPIKE_1H),
            ("spike_24h", 86400, spike_24h if spike_24h is not None else POLYMARKET_SPIKE_24H),
        ]

        session: Session = self._db.get_session()
        try:
            # 获取要回测的市场
            if condition_ids:
                events = session.query(PolymarketEvent).filter(
                    PolymarketEvent.condition_id.in_(condition_ids)
                ).all()
            else:
                events = session.query(PolymarketEvent).filter(
                    PolymarketEvent.is_active == False  # noqa: E712
                ).order_by(PolymarketEvent.volume.desc()).limit(50).all()

            if not events:
                task_manager.update_progress(task_id, 100, "无可回测的市场")
                return {"total_markets": 0, "alerts": [], "summary": {}}

            total_markets = len(events)
            all_alerts = []
            market_results = []

            for idx, event in enumerate(events):
                progress = 10 + int(80 * (idx / total_markets))
                task_manager.update_progress(
                    task_id, progress,
                    f"回测 ({idx + 1}/{total_markets}): {event.question[:40]}..."
                )

                # 获取价格序列
                snapshots = session.query(PolymarketPriceSnapshot).filter(
                    PolymarketPriceSnapshot.condition_id == event.condition_id
                ).order_by(PolymarketPriceSnapshot.timestamp.asc()).all()

                if len(snapshots) < 2:
                    continue

                # 回放价格序列，检测 spike
                alerts = self._replay_market(
                    event=event,
                    snapshots=snapshots,
                    rules=rules,
                    use_llm=use_llm,
                )

                market_results.append({
                    "condition_id": event.condition_id,
                    "question": event.question,
                    "category": event.category,
                    "volume": event.volume,
                    "data_points": len(snapshots),
                    "alerts_triggered": len(alerts),
                    "price_start": snapshots[0].yes_price,
                    "price_end": snapshots[-1].yes_price,
                    "price_range": max(s.yes_price for s in snapshots) - min(s.yes_price for s in snapshots),
                    "time_start": snapshots[0].timestamp.isoformat() if snapshots[0].timestamp else None,
                    "time_end": snapshots[-1].timestamp.isoformat() if snapshots[-1].timestamp else None,
                })

                all_alerts.extend(alerts)

            # 生成汇总统计
            summary = self._compute_summary(market_results, all_alerts)

            task_manager.update_progress(
                task_id, 95,
                f"回测完成: {total_markets} 个市场, {len(all_alerts)} 个告警"
            )

            return {
                "total_markets": total_markets,
                "markets": market_results,
                "alerts": all_alerts,
                "summary": summary,
            }
        finally:
            session.close()

    def _replay_market(
        self,
        event: PolymarketEvent,
        snapshots: list[PolymarketPriceSnapshot],
        rules: list[tuple],
        use_llm: bool,
    ) -> list[dict]:
        """回放单个市场的价格序列，检测 Spike。"""
        # 滚动窗口：(timestamp_unix, price)
        price_history: deque[tuple[float, float]] = deque()
        max_age = 86400  # 24h

        alerts = []
        # 冷却缓存
        cooldown: dict[tuple[str, str], float] = {}

        for snap in snapshots:
            ts = snap.timestamp.timestamp() if snap.timestamp else 0
            price = snap.yes_price
            if ts == 0 or price is None:
                continue

            # 添加到滚动窗口
            price_history.append((ts, price))

            # 修剪过期数据
            cutoff = ts - max_age
            while price_history and price_history[0][0] < cutoff:
                price_history.popleft()

            # 检测每档 Spike
            for alert_type, lookback_seconds, threshold in rules:
                target_ts = ts - lookback_seconds
                old_price = self._find_nearest_price(price_history, target_ts, lookback_seconds)
                if old_price is None:
                    continue

                change = price - old_price
                if abs(change) < threshold:
                    continue

                # 冷却检查
                cache_key = (event.condition_id, alert_type)
                last_trigger = cooldown.get(cache_key, 0)
                if ts - last_trigger < POLYMARKET_LLM_COOLDOWN:
                    continue
                cooldown[cache_key] = ts

                alert_data = {
                    "condition_id": event.condition_id,
                    "question": event.question,
                    "category": event.category,
                    "alert_type": alert_type,
                    "price_before": round(old_price, 4),
                    "price_after": round(price, 4),
                    "price_change": round(change, 4),
                    "timeframe_seconds": lookback_seconds,
                    "timestamp": snap.timestamp.isoformat() if snap.timestamp else None,
                    "affected_tickers": [],
                    "affected_a_shares": [],
                    "affected_sectors": [],
                    "affected_sw_industries": [],
                    "llm_summary": None,
                    "llm_sentiment": None,
                    "llm_confidence": None,
                }

                # LLM 分析
                if use_llm and self._analyzer.is_available():
                    try:
                        result = self._analyzer.analyze({
                            "question": event.question,
                            "description": event.description or "",
                            "category": event.category or "",
                            "price_before": old_price,
                            "price_after": price,
                            "price_change": change,
                            "alert_type": alert_type,
                            "timeframe_seconds": lookback_seconds,
                        })
                        if result:
                            alert_data["affected_tickers"] = result.get("affected_tickers", [])
                            alert_data["affected_a_shares"] = result.get("affected_a_shares", [])
                            alert_data["affected_sectors"] = result.get("affected_sectors", [])
                            alert_data["affected_sw_industries"] = result.get("affected_sw_industries", [])
                            alert_data["llm_summary"] = result.get("summary")
                            alert_data["llm_sentiment"] = result.get("overall_sentiment")
                            alert_data["llm_confidence"] = result.get("confidence")
                    except Exception as e:
                        logger.warning(f"LLM 分析失败: {e}")

                alerts.append(alert_data)
                logger.info(
                    f"[回测] {alert_type} | {event.question[:40]} | "
                    f"{old_price:.2%} -> {price:.2%} ({change:+.2%})"
                )

        return alerts

    @staticmethod
    def _find_nearest_price(
        history: deque[tuple[float, float]],
        target_ts: float,
        window_seconds: int,
    ) -> Optional[float]:
        """在价格历史中找到最接近 target_ts 的价格。"""
        if not history:
            return None

        best = None
        best_diff = float("inf")
        for ts, price in history:
            diff = abs(ts - target_ts)
            if diff < best_diff:
                best_diff = diff
                best = price

        # 容差：窗口的 20%
        if best_diff > window_seconds * 0.2:
            return None
        return best

    @staticmethod
    def _compute_summary(market_results: list[dict], alerts: list[dict]) -> dict:
        """计算回测汇总统计。"""
        if not market_results:
            return {}

        total_markets = len(market_results)
        markets_with_alerts = sum(1 for m in market_results if m["alerts_triggered"] > 0)
        total_alerts = len(alerts)

        # 按告警类型统计
        alert_type_counts = {}
        for a in alerts:
            t = a["alert_type"]
            alert_type_counts[t] = alert_type_counts.get(t, 0) + 1

        # 按分类统计
        category_counts = {}
        for a in alerts:
            c = a.get("category", "unknown")
            category_counts[c] = category_counts.get(c, 0) + 1

        # LLM 分析统计
        alerts_with_llm = sum(1 for a in alerts if a.get("llm_summary"))
        sentiment_values = [a["llm_sentiment"] for a in alerts if a.get("llm_sentiment") is not None]
        avg_sentiment = sum(sentiment_values) / len(sentiment_values) if sentiment_values else None
        avg_confidence = None
        confidence_values = [a["llm_confidence"] for a in alerts if a.get("llm_confidence") is not None]
        if confidence_values:
            avg_confidence = sum(confidence_values) / len(confidence_values)

        # 受影响股票汇总
        us_ticker_freq: dict[str, int] = {}
        a_share_freq: dict[str, int] = {}
        for a in alerts:
            for t in a.get("affected_tickers", []):
                ticker = t.get("ticker", "")
                if ticker:
                    us_ticker_freq[ticker] = us_ticker_freq.get(ticker, 0) + 1
            for s in a.get("affected_a_shares", []):
                name = s.get("name", "")
                if name:
                    a_share_freq[name] = a_share_freq.get(name, 0) + 1

        # 排序取 Top 10
        top_us = sorted(us_ticker_freq.items(), key=lambda x: -x[1])[:10]
        top_a = sorted(a_share_freq.items(), key=lambda x: -x[1])[:10]

        # 价格波动统计
        avg_price_range = sum(m["price_range"] for m in market_results) / total_markets

        return {
            "total_markets": total_markets,
            "markets_with_alerts": markets_with_alerts,
            "total_alerts": total_alerts,
            "alert_type_counts": alert_type_counts,
            "category_counts": category_counts,
            "alerts_with_llm": alerts_with_llm,
            "avg_sentiment": round(avg_sentiment, 4) if avg_sentiment is not None else None,
            "avg_confidence": round(avg_confidence, 4) if avg_confidence is not None else None,
            "top_us_tickers": [{"ticker": t, "count": c} for t, c in top_us],
            "top_a_shares": [{"name": n, "count": c} for n, c in top_a],
            "avg_price_range": round(avg_price_range, 4),
        }
