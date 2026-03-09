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
from concurrent.futures import ThreadPoolExecutor, as_completed
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

LLM_CONCURRENCY = 8  # Polymarket 回测 LLM 并发数
BACKTEST_EXCLUDE_CATEGORIES = {"sports", "pop-culture", "crypto"}  # 已迁移到 is_excluded 字段，保留供参考
RESOLUTION_YES_THRESHOLD = 0.90  # 价格 ≥ 此值视为 YES 出结果
RESOLUTION_NO_THRESHOLD = 0.10   # 价格 ≤ 此值视为 NO 出结果

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
        exclude_categories: Optional[set[str]] = None,
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
        logger.info("[回测] 初始化回测引擎，加载历史数据...")
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
                    PolymarketEvent.is_excluded == False  # noqa: E712
                ).order_by(PolymarketEvent.volume.desc()).all()
                logger.info(f"[回测] 已排除 is_excluded=True 的事件")

            if not events:
                logger.warning("[回测] 未找到可回测的市场")
                task_manager.update_progress(task_id, 100, "无可回测的市场")
                return {"total_markets": 0, "alerts": [], "summary": {}}

            total_markets = len(events)
            all_alerts: list[dict] = []
            market_results = []
            logger.info(f"[回测] 找到 {total_markets} 个市场，开始回测...")
            task_manager.update_progress(task_id, 8, f"找到 {total_markets} 个市场，开始回测...")

            # ── 阶段 1: 快速扫描全部市场，收集 spike ──
            # alerts_with_ts: [(alert_data, snap_timestamp), ...]
            alerts_with_ts: list[tuple[dict, datetime]] = []

            for idx, event in enumerate(events):
                progress = 10 + int(50 * (idx / total_markets))
                task_manager.update_progress(
                    task_id, progress,
                    f"扫描 ({idx + 1}/{total_markets}): {event.question[:40]}..."
                )
                logger.info(f"[回测] [{idx + 1}/{total_markets}] {event.question[:60]}")

                snapshots = session.query(PolymarketPriceSnapshot).filter(
                    PolymarketPriceSnapshot.condition_id == event.condition_id
                ).order_by(PolymarketPriceSnapshot.timestamp.asc()).all()

                if len(snapshots) < 2:
                    logger.warning(f"[回测]   跳过: 数据点不足 ({len(snapshots)})")
                    market_results.append(self._market_stat(event, snapshots, 0))
                    continue
                logger.info(f"[回测]   价格序列: {len(snapshots)} 个数据点")

                spikes = self._replay_market(event, snapshots, rules)

                if spikes:
                    logger.info(f"[回测]   检测到 {len(spikes)} 个 spike")
                for alert_data, snap_ts in spikes:
                    alerts_with_ts.append((alert_data, snap_ts))

                market_results.append(self._market_stat(event, snapshots, len(spikes)))

            total_spikes = len(alerts_with_ts)
            n_spike = sum(1 for a, _ in alerts_with_ts if a["alert_type"].startswith("spike"))
            n_resolved = total_spikes - n_spike
            logger.info(f"[回测] 阶段 1 完成: {total_spikes} 个 alert (spike {n_spike}, resolution {n_resolved})")
            task_manager.update_progress(task_id, 60, f"扫描完成: spike {n_spike} + resolution {n_resolved} = {total_spikes} 个 alert")

            # ── 阶段 2: 并发 LLM 分析（跳过 DB 中已有 LLM 结果的 alert）──
            if use_llm and self._analyzer.is_available() and alerts_with_ts:
                # 查询 DB 中已有 LLM 分析的 alert，用于跳过
                existing_keys: set[tuple] = set()
                try:
                    existing = session.query(
                        PolymarketAlert.condition_id,
                        PolymarketAlert.alert_type,
                        PolymarketAlert.created_at,
                    ).filter(
                        PolymarketAlert.llm_summary.isnot(None),
                    ).all()
                    existing_keys = {(r.condition_id, r.alert_type, r.created_at) for r in existing}
                except Exception:
                    pass

                pending = []
                skipped_items = []
                for item in alerts_with_ts:
                    alert_data, snap_ts = item
                    key = (alert_data["condition_id"], alert_data["alert_type"], snap_ts)
                    if key in existing_keys:
                        skipped_items.append(item)
                    else:
                        pending.append(item)

                skipped = len(skipped_items)
                total_pending = len(pending)
                logger.info(
                    f"[回测] 阶段 2: {total_spikes} 个 alert, "
                    f"已有 LLM 结果 {skipped} 个(跳过), 待分析 {total_pending} 个 (concurrency={LLM_CONCURRENCY})"
                )

                # 从 DB 回填已有结果到 skipped_items，同时构建 slug → LLM 结果缓存
                import json as _json

                def _pj(val):
                    if not val:
                        return []
                    try:
                        return _json.loads(val)
                    except (ValueError, TypeError):
                        return []

                def _apply_result(alert_data: dict, result: dict):
                    """将 LLM 结果写入 alert_data。"""
                    alert_data["affected_tickers"] = result.get("affected_tickers", [])
                    alert_data["affected_a_shares"] = result.get("affected_a_shares", [])
                    alert_data["affected_sectors"] = result.get("affected_sectors", [])
                    alert_data["affected_sw_industries"] = result.get("affected_sw_industries", [])
                    alert_data["llm_summary"] = result.get("summary")
                    alert_data["llm_sentiment"] = result.get("overall_sentiment")
                    alert_data["llm_confidence"] = result.get("confidence")

                def _row_to_result(row) -> dict:
                    return {
                        "affected_tickers": _pj(row.affected_tickers),
                        "affected_a_shares": _pj(row.affected_a_shares),
                        "affected_sectors": _pj(row.affected_sectors),
                        "affected_sw_industries": _pj(row.affected_sw_industries),
                        "summary": row.llm_summary,
                        "overall_sentiment": row.llm_sentiment,
                        "confidence": row.llm_confidence,
                    }

                # slug → LLM 结果缓存（跨 condition_id 复用）
                slug_llm_cache: dict[str, dict] = {}

                if skipped_items:
                    db_alerts = session.query(PolymarketAlert).filter(
                        PolymarketAlert.llm_summary.isnot(None),
                    ).all()
                    db_map = {}
                    for r in db_alerts:
                        db_map[(r.condition_id, r.alert_type, r.created_at)] = r
                    for item in skipped_items:
                        alert_data, snap_ts = item
                        key = (alert_data["condition_id"], alert_data["alert_type"], snap_ts)
                        row = db_map.get(key)
                        if row:
                            alert_data["affected_tickers"] = _pj(row.affected_tickers)
                            alert_data["affected_a_shares"] = _pj(row.affected_a_shares)
                            alert_data["affected_sectors"] = _pj(row.affected_sectors)
                            alert_data["affected_sw_industries"] = _pj(row.affected_sw_industries)
                            alert_data["llm_summary"] = row.llm_summary
                            alert_data["llm_sentiment"] = row.llm_sentiment
                            alert_data["llm_confidence"] = row.llm_confidence
                            # 缓存到 slug 级别，供 pending alerts 复用
                            slug = alert_data.get("slug", "") or alert_data.get("condition_id", "")
                            if slug and slug not in slug_llm_cache:
                                slug_llm_cache[slug] = _row_to_result(row)

                # ── 用 slug 缓存预填充 pending alerts ──
                still_pending = []
                slug_cache_hits = 0
                for item in pending:
                    alert_data, snap_ts = item
                    slug = alert_data.get("slug", "") or alert_data.get("condition_id", "")
                    cached = slug_llm_cache.get(slug)
                    if cached:
                        _apply_result(alert_data, cached)
                        skipped_items.append(item)
                        slug_cache_hits += 1
                    else:
                        still_pending.append(item)

                if slug_cache_hits:
                    logger.info(f"[回测] Slug 缓存命中: {slug_cache_hits} 个 alert 复用已有 LLM 结果")

                # ── Resolution alerts 不调 LLM，从同 slug 的 spike 分析复用 ──
                pending_spikes = []
                pending_resolutions = []
                for item in still_pending:
                    if item[0]["alert_type"].startswith("resolved"):
                        pending_resolutions.append(item)
                    else:
                        pending_spikes.append(item)

                done_count = 0

                # ── Slug 聚合：仅对 spike alerts 调 LLM ──
                slug_groups: dict[str, list[tuple[dict, datetime]]] = {}
                for item in pending_spikes:
                    slug = item[0].get("slug", "") or item[0].get("condition_id", "")
                    slug_groups.setdefault(slug, []).append(item)

                # 每个 slug 选一个代表（price_change 绝对值最大的）
                slug_representatives: list[tuple[dict, datetime]] = []
                slug_siblings: dict[str, list[tuple[dict, datetime]]] = {}
                for slug, items in slug_groups.items():
                    items.sort(key=lambda x: abs(x[0].get("price_change", 0)), reverse=True)
                    slug_representatives.append(items[0])
                    slug_siblings[slug] = items[1:]

                actual_llm_calls = len(slug_representatives)
                n_res_skipped = len(pending_resolutions)
                sibling_count = len(pending_spikes) - actual_llm_calls
                logger.info(
                    f"[回测] Slug 聚合: {total_pending} alerts → {actual_llm_calls} 次 LLM 调用 "
                    f"(slug缓存 {slug_cache_hits}, slug聚合 {sibling_count}, "
                    f"resolution跳过 {n_res_skipped})"
                )
                task_manager.update_progress(
                    task_id, 63,
                    f"LLM 分析: {actual_llm_calls} 次调用 "
                    f"(缓存{slug_cache_hits}+聚合{sibling_count}+resolution{n_res_skipped})..."
                )

                def _analyze_one(item: tuple[dict, datetime]) -> tuple[dict, datetime, Optional[dict]]:
                    alert_data, snap_ts = item
                    result = None
                    try:
                        result = self._analyzer.analyze({
                            "question": alert_data["question"],
                            "description": "",
                            "category": alert_data.get("category", ""),
                            "price_before": alert_data["price_before"],
                            "price_after": alert_data["price_after"],
                            "price_change": alert_data["price_change"],
                            "alert_type": alert_data["alert_type"],
                            "timeframe_seconds": alert_data["timeframe_seconds"],
                        })
                        if result:
                            _apply_result(alert_data, result)
                            us_tickers = [t.get("ticker", "") for t in result.get("affected_tickers", [])[:3]]
                            a_shares = [s.get("name", "") for s in result.get("affected_a_shares", [])[:3]]
                            logger.info(
                                f"[回测] LLM 完成: {alert_data['alert_type']} | "
                                f"sentiment={result.get('overall_sentiment', '-')}"
                                + (f" | 美股: {', '.join(us_tickers)}" if us_tickers else "")
                                + (f" | A股: {', '.join(a_shares)}" if a_shares else "")
                            )
                    except Exception as e:
                        logger.warning(f"[回测] LLM 分析失败: {e}")
                    return alert_data, snap_ts, result

                analyzed: list[tuple[dict, datetime]] = list(skipped_items)
                if slug_representatives:
                    with ThreadPoolExecutor(max_workers=LLM_CONCURRENCY) as pool:
                        futures = {pool.submit(_analyze_one, item): item for item in slug_representatives}
                        for future in as_completed(futures):
                            alert_data, snap_ts, result = future.result()
                            analyzed.append((alert_data, snap_ts))
                            self._persist_alert(alert_data, snap_ts, source="backtest")

                            # 将结果复制给同 slug 的 spike 兄弟 alerts
                            slug = alert_data.get("slug", "") or alert_data.get("condition_id", "")
                            if result:
                                slug_llm_cache[slug] = result
                                if slug in slug_siblings:
                                    for sib_data, sib_ts in slug_siblings[slug]:
                                        _apply_result(sib_data, result)
                                        analyzed.append((sib_data, sib_ts))
                                        self._persist_alert(sib_data, sib_ts, source="backtest")

                            done_count += 1
                            if done_count % 50 == 0 or done_count == actual_llm_calls:
                                pct = 62 + int(28 * done_count / actual_llm_calls)
                                task_manager.update_progress(
                                    task_id, pct,
                                    f"LLM 分析 {done_count}/{actual_llm_calls}..."
                                )
                                logger.info(f"[回测] LLM 进度: {done_count}/{actual_llm_calls}")

                # ── Resolution alerts: 从 slug_llm_cache 复用，不调 LLM ──
                res_filled = 0
                for item in pending_resolutions:
                    alert_data, snap_ts = item
                    slug = alert_data.get("slug", "") or alert_data.get("condition_id", "")
                    cached = slug_llm_cache.get(slug)
                    if cached:
                        _apply_result(alert_data, cached)
                        res_filled += 1
                    analyzed.append(item)
                    self._persist_alert(alert_data, snap_ts, source="backtest")

                if pending_resolutions:
                    logger.info(
                        f"[回测] Resolution alerts: {len(pending_resolutions)} 个, "
                        f"从缓存复用 {res_filled} 个, 无 LLM {len(pending_resolutions) - res_filled} 个"
                    )

                alerts_with_ts = analyzed

            # 收集最终 alert 列表
            for alert_data, snap_ts in alerts_with_ts:
                all_alerts.append(alert_data)

            # 回填 market_results 的 alerts_triggered（LLM 可能没改数量，但保持一致）
            alerts_per_market: dict[str, int] = {}
            for a in all_alerts:
                cid = a["condition_id"]
                alerts_per_market[cid] = alerts_per_market.get(cid, 0) + 1
            for m in market_results:
                m["alerts_triggered"] = alerts_per_market.get(m["condition_id"], 0)

            # 生成汇总统计
            summary = self._compute_summary(market_results, all_alerts)

            msg = f"回测完成: {total_markets} 个市场, {len(all_alerts)} 个告警"
            logger.info(f"[回测] {msg}")
            task_manager.update_progress(task_id, 95, msg)

            return {
                "total_markets": total_markets,
                "markets": market_results,
                "alerts": all_alerts,
                "summary": summary,
            }
        finally:
            session.close()

    @staticmethod
    def _market_stat(event: PolymarketEvent, snapshots: list, n_alerts: int) -> dict:
        if not snapshots or len(snapshots) < 2:
            return {
                "condition_id": event.condition_id,
                "question": event.question,
                "category": event.category,
                "volume": event.volume,
                "data_points": len(snapshots),
                "alerts_triggered": n_alerts,
                "price_start": None, "price_end": None, "price_range": 0,
                "time_start": None, "time_end": None,
            }
        return {
            "condition_id": event.condition_id,
            "question": event.question,
            "category": event.category,
            "volume": event.volume,
            "data_points": len(snapshots),
            "alerts_triggered": n_alerts,
            "price_start": snapshots[0].yes_price,
            "price_end": snapshots[-1].yes_price,
            "price_range": max(s.yes_price for s in snapshots) - min(s.yes_price for s in snapshots),
            "time_start": snapshots[0].timestamp.isoformat() if snapshots[0].timestamp else None,
            "time_end": snapshots[-1].timestamp.isoformat() if snapshots[-1].timestamp else None,
        }

    def _replay_market(
        self,
        event: PolymarketEvent,
        snapshots: list[PolymarketPriceSnapshot],
        rules: list[tuple],
    ) -> list[tuple[dict, datetime]]:
        """回放价格序列，仅做 Spike 检测（不调 LLM）。返回 [(alert_data, snap_timestamp), ...]。"""
        price_history: deque[tuple[float, float]] = deque()
        max_age = 86400

        results: list[tuple[dict, datetime]] = []
        # 冷却键: condition_id（不再区分 alert_type，同一市场共享冷却）
        cooldown: dict[str, float] = {}

        for snap in snapshots:
            ts = snap.timestamp.timestamp() if snap.timestamp else 0
            price = snap.yes_price
            if ts == 0 or price is None:
                continue

            price_history.append((ts, price))

            cutoff = ts - max_age
            while price_history and price_history[0][0] < cutoff:
                price_history.popleft()

            # 冷却检查：同一市场在冷却期内只触发一次（不区分 alert_type）
            cache_key = event.condition_id
            last_trigger = cooldown.get(cache_key, 0)
            if ts - last_trigger < POLYMARKET_LLM_COOLDOWN:
                continue

            # 只保留最显著的一档 spike（与实时监控一致）
            best_spike = None
            best_abs_change = 0.0

            for alert_type, lookback_seconds, threshold in rules:
                target_ts = ts - lookback_seconds
                old_price = self._find_nearest_price(price_history, target_ts, lookback_seconds)
                if old_price is None:
                    continue

                change = price - old_price
                if abs(change) >= threshold and abs(change) > best_abs_change:
                    best_abs_change = abs(change)
                    best_spike = (alert_type, old_price, change, lookback_seconds)

            if not best_spike:
                continue

            alert_type, old_price, change, lookback_seconds = best_spike
            cooldown[cache_key] = ts

            logger.info(
                f"[回测]   {alert_type}: {old_price:.2%} → {price:.2%} ({change:+.2%})"
            )

            alert_data = {
                "condition_id": event.condition_id,
                "question": event.question,
                "category": event.category,
                "slug": event.slug or "",
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

            results.append((alert_data, snap.timestamp))

        # ── Resolution 检测: 事件出结果 ──
        if len(snapshots) >= 2:
            final_price = snapshots[-1].yes_price
            first_price = snapshots[0].yes_price

            resolved_yes = final_price is not None and final_price >= RESOLUTION_YES_THRESHOLD
            resolved_no = final_price is not None and final_price <= RESOLUTION_NO_THRESHOLD

            if resolved_yes or resolved_no:
                threshold = RESOLUTION_YES_THRESHOLD if resolved_yes else RESOLUTION_NO_THRESHOLD
                alert_type = "resolved_yes" if resolved_yes else "resolved_no"

                # 找到价格首次越过阈值的时刻
                resolution_snap = None
                for snap in snapshots:
                    p = snap.yes_price
                    if p is None:
                        continue
                    if resolved_yes and p >= threshold:
                        resolution_snap = snap
                        break
                    if resolved_no and p <= threshold:
                        resolution_snap = snap
                        break

                if resolution_snap:
                    # price_before: 出结果前 24h 的价格，回退到序列首价
                    res_ts = resolution_snap.timestamp.timestamp()
                    before_price = first_price
                    for snap in snapshots:
                        snap_ts = snap.timestamp.timestamp()
                        if snap_ts >= res_ts:
                            break
                        if res_ts - snap_ts <= 86400:
                            before_price = snap.yes_price
                            break

                    change = resolution_snap.yes_price - before_price
                    logger.info(
                        f"[回测]   {alert_type}: {before_price:.2%} → {resolution_snap.yes_price:.2%} ({change:+.2%})"
                    )

                    alert_data = {
                        "condition_id": event.condition_id,
                        "question": event.question,
                        "category": event.category,
                        "slug": event.slug or "",
                        "alert_type": alert_type,
                        "price_before": round(before_price, 4),
                        "price_after": round(resolution_snap.yes_price, 4),
                        "price_change": round(change, 4),
                        "timeframe_seconds": 0,
                        "timestamp": resolution_snap.timestamp.isoformat() if resolution_snap.timestamp else None,
                        "affected_tickers": [],
                        "affected_a_shares": [],
                        "affected_sectors": [],
                        "affected_sw_industries": [],
                        "llm_summary": None,
                        "llm_sentiment": None,
                        "llm_confidence": None,
                    }
                    results.append((alert_data, resolution_snap.timestamp))

        return results

    def _persist_alert(self, alert_data: dict, timestamp, source: str = "backtest"):
        """将回测 alert 写入 polymarket_alert 表（单条，仅 LLM 代表用）。"""
        self._persist_alerts_batch([(alert_data, timestamp)], source=source)

    def _persist_alerts_batch(self, items: list[tuple[dict, ...]], source: str = "backtest"):
        """批量将回测 alerts 写入 polymarket_alert 表。"""
        if not items:
            return
        import json as _json

        session = self._db.get_session()
        try:
            # 一次性查出已存在的 keys
            from sqlalchemy import tuple_
            existing_keys: set[tuple] = set()
            # 分批查询避免 SQL 过长
            batch_size = 500
            for i in range(0, len(items), batch_size):
                batch = items[i:i + batch_size]
                conditions = [(it[0]["condition_id"], it[0]["alert_type"], it[1]) for it in batch]
                rows = session.query(
                    PolymarketAlert.condition_id,
                    PolymarketAlert.alert_type,
                    PolymarketAlert.created_at,
                ).filter(
                    tuple_(
                        PolymarketAlert.condition_id,
                        PolymarketAlert.alert_type,
                        PolymarketAlert.created_at,
                    ).in_(conditions)
                ).all()
                existing_keys.update((r[0], r[1], r[2]) for r in rows)

            new_records = []
            for alert_data, timestamp in items:
                key = (alert_data["condition_id"], alert_data["alert_type"], timestamp)
                if key in existing_keys:
                    continue
                new_records.append(PolymarketAlert(
                    condition_id=alert_data["condition_id"],
                    alert_type=alert_data["alert_type"],
                    price_before=alert_data.get("price_before"),
                    price_after=alert_data.get("price_after"),
                    price_change=alert_data.get("price_change"),
                    timeframe_seconds=alert_data.get("timeframe_seconds"),
                    question=alert_data.get("question"),
                    affected_tickers=_json.dumps(alert_data.get("affected_tickers", []), ensure_ascii=False),
                    affected_a_shares=_json.dumps(alert_data.get("affected_a_shares", []), ensure_ascii=False),
                    affected_sectors=_json.dumps(alert_data.get("affected_sectors", []), ensure_ascii=False),
                    affected_sw_industries=_json.dumps(alert_data.get("affected_sw_industries", []), ensure_ascii=False),
                    llm_summary=alert_data.get("llm_summary"),
                    llm_sentiment=alert_data.get("llm_sentiment"),
                    llm_confidence=alert_data.get("llm_confidence"),
                    is_read=False,
                    created_at=timestamp,
                ))

            if new_records:
                session.bulk_save_objects(new_records)
                session.commit()
                logger.info(f"[回测] 批量持久化 {len(new_records)} 条 alerts (跳过 {len(items) - len(new_records)} 条已存在)")
            else:
                logger.info(f"[回测] 全部 {len(items)} 条 alerts 已存在，跳过")
        except Exception as e:
            session.rollback()
            logger.warning(f"批量持久化 alerts 失败: {e}")
        finally:
            session.close()

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
