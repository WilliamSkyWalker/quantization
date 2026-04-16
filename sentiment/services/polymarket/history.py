"""
Polymarket 历史数据下载器

从 CLOB API 下载已结算市场的历史赔率数据，用于回测。

数据源:
- Gamma API: 发现已结算的高交易量市场
- CLOB API /prices-history: 下载分钟级赔率时间序列
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from sqlalchemy.orm import Session

from services.config import POLYMARKET_GAMMA_API, POLYMARKET_MIN_VOLUME, LOG_LEVEL
# DatabaseManager 已废弃
from sentiment.services.polymarket.models import PolymarketEvent, PolymarketPriceSnapshot
from sentiment.services.polymarket.utils import category_from_tags, is_noise_slug, EXCLUDED_CATEGORIES
from tasks.manager import task_manager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

CLOB_BASE = "https://clob.polymarket.com"
# CLOB API rate limit: 9000 req/10s, we stay conservative
CLOB_REQUEST_INTERVAL = 0.05  # 50ms between requests


def _safe_float(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (ValueError, TypeError):
        return None


def _safe_int(v):
    try:
        return int(v) if v is not None and v != "" else None
    except (ValueError, TypeError):
        return None


def _safe_bool(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("true", "1", "yes")
    return bool(v)


def _safe_dt(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _parse_json_field(v):
    """API 字段可能是 JSON 字符串，也可能已经是 list/dict。"""
    if v is None or v == "":
        return None
    if isinstance(v, (list, dict)):
        return v
    if isinstance(v, str):
        import json as _json
        try:
            return _json.loads(v)
        except (ValueError, TypeError):
            return None
    return None


def _build_full_event_payload(event: dict, market: dict, category: str, excluded: bool, condition_id: str, token_id: str, yes_price: float, volume: float) -> dict:
    """从 Gamma event + market 构建 PolymarketEvent 完整字段字典。"""
    return {
        # 核心字段
        "condition_id": condition_id,
        "token_id": token_id,
        "question": (market.get("question") or event.get("title") or "")[:1000],
        "description": (market.get("description") or event.get("description") or "")[:5000],
        "category": category,
        "outcome_yes_price": yes_price,
        "outcome_no_price": 1.0 - yes_price if yes_price is not None else None,
        "volume": volume,
        "liquidity": _safe_float(market.get("liquidity")),
        "end_date": _safe_dt(market.get("endDate")),
        "is_active": False,
        "is_excluded": excluded,
        "slug": event.get("slug", ""),
        "gamma_market_id": str(market.get("id", "")),
        # Event 层
        "event_id": str(event.get("id", "")),
        "event_ticker": event.get("ticker"),
        "title": (event.get("title") or "")[:1000],
        "tags": event.get("tags"),
        "open_interest": _safe_float(event.get("openInterest")),
        "volume_1wk": _safe_float(event.get("volume1wk")),
        "volume_1mo": _safe_float(event.get("volume1mo")),
        "volume_1yr": _safe_float(event.get("volume1yr")),
        "neg_risk": _safe_bool(event.get("negRisk")),
        "neg_risk_market_id": event.get("negRiskMarketID"),
        "comment_count": _safe_int(event.get("commentCount")),
        "closed_time": _safe_dt(event.get("closedTime")),
        "start_date": _safe_dt(event.get("startDate")),
        "restricted": _safe_bool(event.get("restricted")),
        "archived": _safe_bool(event.get("archived")),
        # Market 层
        "outcomes": _parse_json_field(market.get("outcomes")),
        "outcome_prices": _parse_json_field(market.get("outcomePrices")),
        "best_bid": _safe_float(market.get("bestBid")),
        "best_ask": _safe_float(market.get("bestAsk")),
        "spread": _safe_float(market.get("spread")),
        "last_trade_price": _safe_float(market.get("lastTradePrice")),
        "volume_clob": _safe_float(market.get("volumeClob")),
        "volume_num": _safe_float(market.get("volumeNum")),
        "one_day_price_change": _safe_float(market.get("oneDayPriceChange")),
        "one_hour_price_change": _safe_float(market.get("oneHourPriceChange")),
        "one_week_price_change": _safe_float(market.get("oneWeekPriceChange")),
        "one_month_price_change": _safe_float(market.get("oneMonthPriceChange")),
        "one_year_price_change": _safe_float(market.get("oneYearPriceChange")),
        "uma_bond": str(market.get("umaBond")) if market.get("umaBond") else None,
        "uma_reward": str(market.get("umaReward")) if market.get("umaReward") else None,
        "maker_base_fee": _safe_float(market.get("makerBaseFee")),
        "taker_base_fee": _safe_float(market.get("takerBaseFee")),
        "market_type": market.get("marketType"),
        "market_closed": _safe_bool(market.get("closed")),
        "market_active": _safe_bool(market.get("active")),
    }


class PolymarketHistoryDownloader:
    """下载 Polymarket 已结算市场的历史赔率数据。"""

    def __init__(self):
        self._db = None  # DatabaseManager 已废弃

    def discover_resolved_markets(
        self,
        task_id: str,
        limit: int = 0,
        min_volume: int = 0,
        exclude_categories: Optional[set[str]] = None,
    ) -> list[dict]:
        """
        从 Gamma API 发现已结算的高交易量市场。

        Args:
            limit: 最大获取 event 数，0 = 全部（自动分页）
            min_volume: 最低交易量过滤
            exclude_categories: 排除的分类（默认排除 sports）

        Returns:
            已结算市场列表 [{condition_id, token_id, question, ...}]
        """
        self._db.init_tables()
        task_manager.update_progress(task_id, 5, "正在从 Gamma API 获取已结算市场...")

        if exclude_categories is None:
            exclude_categories = EXCLUDED_CATEGORIES

        url = f"{POLYMARKET_GAMMA_API}/events"
        page_size = 500
        vol_threshold = min_volume or POLYMARKET_MIN_VOLUME

        # 分页获取所有已结算 events
        all_events = []
        offset = 0
        while True:
            params = {
                "closed": "true",
                "order": "volume",
                "ascending": "false",
                "limit": page_size,
                "offset": offset,
            }
            try:
                resp = requests.get(url, params=params, timeout=60)
                resp.raise_for_status()
                events = resp.json()
            except Exception as e:
                logger.warning(f"Gamma API 请求失败 (offset={offset}): {e}")
                break

            if not events:
                logger.debug("discover_resolved_markets: 当前页返回空数据，分页结束")
                break

            all_events.extend(events)
            offset += page_size
            task_manager.update_progress(
                task_id, 5, f"获取已结算事件 {len(all_events)}..."
            )
            logger.info(f"[发现] offset={offset - page_size}: {len(events)} events (累计 {len(all_events)})")

            if len(events) < page_size:
                logger.debug(f"discover_resolved_markets: 当前页 {len(events)} < page_size {page_size}，分页结束")
                break
            # 如果有 limit 限制
            if limit > 0 and len(all_events) >= limit:
                all_events = all_events[:limit]
                logger.debug(f"discover_resolved_markets: 已达 limit={limit}，分页结束")
                break

            time.sleep(0.1)

        logger.info(f"[发现] Gamma API 共返回 {len(all_events)} 个已结算事件")

        # ===== Pass 1: 内存中过滤 + 构建 payload（不写 DB）=====
        import json as _json
        results = []
        seen_condition_ids = set()
        all_payloads = []  # [(condition_id, payload), ...]

        for event in all_events:
            category = category_from_tags(event.get("tags", []))
            event_slug = event.get("slug", "")
            excluded = bool(category and category in exclude_categories) or is_noise_slug(event_slug)

            for market in event.get("markets", []):
                volume = float(market.get("volume", 0) or 0)
                if volume < vol_threshold:
                    continue

                condition_id = market.get("conditionId") or market.get("condition_id", "")
                if not condition_id or condition_id in seen_condition_ids:
                    continue
                seen_condition_ids.add(condition_id)

                # 解析 token IDs
                raw_tokens = market.get("clobTokenIds", "")
                if isinstance(raw_tokens, str):
                    token_ids = [t.strip('" []') for t in raw_tokens.split(",") if t.strip('" []')]
                elif isinstance(raw_tokens, list):
                    token_ids = [str(t) for t in raw_tokens]
                else:
                    token_ids = []
                token_id = token_ids[0] if token_ids else ""

                # 解析价格
                try:
                    prices = _json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                    yes_price = float(prices[0])
                except (ValueError, IndexError, TypeError):
                    yes_price = 0.5

                if not excluded:
                    results.append({
                        "condition_id": condition_id,
                        "token_id": token_id,
                        "question": market.get("question", event.get("title", "")),
                        "description": market.get("description", event.get("description", "")),
                        "category": category,
                        "yes_price": yes_price,
                        "volume": volume,
                        "liquidity": float(market.get("liquidity", 0) or 0),
                        "slug": event.get("slug", ""),
                        "gamma_market_id": market.get("id", ""),
                        "end_date": market.get("endDate"),
                        "resolved": True,
                    })

                payload = _build_full_event_payload(
                    event, market, category, excluded, condition_id, token_id, yes_price, volume,
                )
                all_payloads.append((condition_id, payload))

        logger.info(f"[过滤] 符合条件 {len(all_payloads)} 个 market（min_volume={vol_threshold}）")
        if not all_payloads:
            return results

        # ===== Pass 2: 走 db.upsert() 异步写入（提交到 50 线程写池）=====
        BATCH_WRITE = 2000
        total = 0
        try:
            for i in range(0, len(all_payloads), BATCH_WRITE):
                batch = all_payloads[i:i + BATCH_WRITE]
                records = [p for _, p in batch]
                self._db.upsert(PolymarketEvent, records, ["condition_id"])
                total += len(batch)
                logger.info(f"[写入] 提交进度 {total}/{len(all_payloads)}")
            logger.info(f"[写入] 全部 {len(all_payloads)} 条已提交到写池，等待 flush...")
            # 等待异步写完成
            from concurrent.futures import wait
            futures = getattr(self._db, "_write_futures", [])
            if futures:
                wait(futures)
                logger.info("[写入] 所有异步写入已完成")
        except Exception as e:
            logger.error(f"discover_resolved_markets: 批量写入失败: {e}")
            raise

        task_manager.update_progress(
            task_id, 20,
            f"发现 {len(results)} 个已结算市场（排除 {', '.join(exclude_categories)}）"
        )
        logger.info(f"已结算市场发现完成: {len(results)} 个（从 {len(all_events)} 个事件中）")
        return results

    def backfill_categories(self) -> dict:
        """从 Gamma API 批量回填所有 category 为空的事件的分类。"""
        self._db.init_tables()
        session: Session = self._db.get_session()
        try:
            # 找出所有 category 为空的 slug
            events_no_cat = session.query(PolymarketEvent).filter(
                (PolymarketEvent.category == None) | (PolymarketEvent.category == "")  # noqa: E711
            ).all()
            if not events_no_cat:
                return {"updated": 0, "total": 0}

            # 按 slug 聚合（同一 event 下多个 market 共享 slug）
            slug_to_cids: dict[str, list] = {}
            for e in events_no_cat:
                slug = e.slug or ""
                slug_to_cids.setdefault(slug, []).append(e)

            # 批量从 Gamma API 查询
            updated = 0
            slugs = [s for s in slug_to_cids if s]
            logger.info(f"[回填分类] {len(events_no_cat)} 个事件待回填, {len(slugs)} 个 slug")

            for slug in slugs:
                try:
                    resp = requests.get(
                        f"{POLYMARKET_GAMMA_API}/events",
                        params={"slug": slug, "limit": 1},
                        timeout=15,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    if data:
                        cat = category_from_tags(data[0].get("tags", []))
                        if cat:
                            is_excl = cat in EXCLUDED_CATEGORIES
                            for ev in slug_to_cids[slug]:
                                ev.category = cat
                                ev.is_excluded = is_excl
                                updated += 1
                    time.sleep(0.05)
                except Exception as exc:
                    logger.debug(f"[回填分类] slug={slug} 失败: {exc}")

            session.commit()
            logger.info(f"[回填分类] 完成: {updated}/{len(events_no_cat)} 更新")
            return {"updated": updated, "total": len(events_no_cat)}
        except Exception as e:
            session.rollback()
            logger.error(f"backfill_categories: 回填分类失败: {e}")
            raise
        finally:
            session.close()

    def download_price_history(
        self,
        task_id: str,
        token_id: str,
        condition_id: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        fidelity: int = 60,
    ) -> list[dict]:
        """
        从 CLOB API 下载单个市场的历史价格数据。

        Args:
            token_id: CLOB token ID
            condition_id: 关联的 condition_id
            start_ts: 开始时间戳 (unix seconds)
            end_ts: 结束时间戳 (unix seconds)
            fidelity: 数据粒度（分钟），默认 60 分钟

        Returns:
            价格时间序列 [{"t": timestamp, "p": price}]
        """
        url = f"{CLOB_BASE}/prices-history"
        params = {"market": token_id, "fidelity": fidelity}
        if start_ts and end_ts:
            params["startTs"] = start_ts
            params["endTs"] = end_ts
        else:
            params["interval"] = "max"

        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        history = data.get("history", [])
        if not history:
            # 已结算市场在细粒度下可能返回空数据，尝试更粗粒度（12h）
            if fidelity < 720:
                params["fidelity"] = 720
                resp2 = requests.get(url, params=params, timeout=15)
                resp2.raise_for_status()
                data = resp2.json()
                history = data.get("history", [])

        if not history:
            logger.warning(f"token {token_id} 无历史数据")
            return []

        # 批量存入 DB
        session: Session = self._db.get_session()
        saved = 0
        try:
            objects = []
            for point in history:
                ts = point.get("t", 0)
                price = float(point.get("p", 0))
                if ts == 0 or price == 0:
                    logger.debug("download_price_history: 数据点 ts 或 price 为 0，跳过")
                    continue
                objects.append(PolymarketPriceSnapshot(
                    condition_id=condition_id,
                    timestamp=datetime.fromtimestamp(ts),
                    yes_price=price,
                    no_price=round(1.0 - price, 4),
                    spread=0.0,
                    volume_24h=0,
                    source="clob_history",
                ))
            if objects:
                session.bulk_save_objects(objects)
                saved = len(objects)
            session.commit()
            logger.info(f"保存 {saved} 条历史快照: {condition_id}")
        except Exception as e:
            session.rollback()
            logger.error(f"download_price_history: 保存历史快照失败: {e}")
            raise
        finally:
            session.close()

        return history

    def _download_one_market(self, market: dict, fidelity: int) -> dict:
        """下载单个市场的历史数据（线程安全）。返回 {condition_id, question, data_points, error}。"""
        token_id = market.get("token_id", "")
        condition_id = market.get("condition_id", "")
        if not token_id:
            return {"condition_id": condition_id, "question": "", "data_points": 0, "error": "no token_id"}

        try:
            history = self.download_price_history(
                task_id="",
                token_id=token_id,
                condition_id=condition_id,
                fidelity=fidelity,
            )
            return {
                "condition_id": condition_id,
                "question": market.get("question", ""),
                "data_points": len(history),
                "error": None,
            }
        except Exception as e:
            return {
                "condition_id": condition_id,
                "question": market.get("question", ""),
                "data_points": 0,
                "error": str(e),
            }

    def download_batch(
        self,
        task_id: str,
        markets: Optional[list[dict]] = None,
        limit: int = 0,
        fidelity: int = 60,
        skip_existing: bool = True,
        concurrency: int = 50,
    ):
        """
        批量下载已结算市场历史数据（多线程并发）。

        完整流程：
        1. 发现已结算市场（如未提供 markets）
        2. 跳过已有快照的市场（skip_existing=True）
        3. 多线程并发下载历史价格
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        self._db.init_tables()

        if markets is None:
            # 优先从 DB 取可用事件（避免重新全量拉 Gamma API）
            session_m: Session = self._db.get_session()
            try:
                db_events = session_m.query(PolymarketEvent).filter(
                    PolymarketEvent.is_excluded == False  # noqa: E712
                ).order_by(PolymarketEvent.volume.desc()).all()
                if db_events:
                    markets = [
                        {"condition_id": e.condition_id, "token_id": e.token_id or "",
                         "question": e.question or ""}
                        for e in db_events if e.token_id
                    ]
                    if limit > 0:
                        markets = markets[:limit]
                    logger.info(f"[下载] 从 DB 获取 {len(markets)} 个可用事件")
                    task_manager.update_progress(task_id, 10, f"从 DB 获取 {len(markets)} 个可用事件")
                else:
                    # DB 为空时回退到 Gamma API
                    markets = self.discover_resolved_markets(task_id, limit=limit)
            finally:
                session_m.close()

        if not markets:
            logger.debug("download_batch: 未发现符合条件的已结算市场，返回空结果")
            task_manager.update_progress(task_id, 100, "未发现符合条件的已结算市场")
            return {"total_markets": 0, "total_snapshots": 0, "skipped": 0}

        # 跳过已有快照的市场
        skipped = 0
        if skip_existing:
            session: Session = self._db.get_session()
            try:
                from sqlalchemy import func
                existing_cids = set(
                    r[0] for r in session.query(PolymarketPriceSnapshot.condition_id)
                    .group_by(PolymarketPriceSnapshot.condition_id)
                    .having(func.count() >= 2)
                    .all()
                )
            finally:
                session.close()
            before = len(markets)
            markets = [m for m in markets if m["condition_id"] not in existing_cids]
            skipped = before - len(markets)
            logger.info(f"[下载] 跳过 {skipped} 个已有数据的市场，剩余 {len(markets)} 待下载")
            task_manager.update_progress(
                task_id, 22,
                f"跳过 {skipped} 个已有数据，待下载 {len(markets)} 个市场"
            )

        if not markets:
            logger.debug(f"download_batch: 全部 {skipped} 个市场已有数据，无需下载")
            task_manager.update_progress(task_id, 100, f"全部 {skipped} 个市场已有数据，无需下载")
            return {"total_markets": 0, "total_snapshots": 0, "skipped": skipped}

        total = len(markets)
        total_snapshots = 0
        downloaded_markets = []
        failed = 0
        done_count = 0

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(self._download_one_market, m, fidelity): m
                for m in markets
            }
            for future in as_completed(futures):
                result = future.result()
                done_count += 1

                if result["error"]:
                    failed += 1
                    if failed <= 10:
                        logger.warning(f"[下载] 失败 {result['condition_id']}: {result['error']}")
                else:
                    total_snapshots += result["data_points"]
                    downloaded_markets.append(result)

                if done_count % 100 == 0 or done_count == total:
                    pct = 22 + int(68 * done_count / total)
                    task_manager.update_progress(
                        task_id, pct,
                        f"下载进度 {done_count}/{total}, 成功 {len(downloaded_markets)}, "
                        f"累计 {total_snapshots} 条数据"
                    )

        msg = f"下载完成: {len(downloaded_markets)}/{total} 个市场, {total_snapshots} 条数据"
        if skipped:
            msg += f", 跳过 {skipped} 个已有数据"
        if failed:
            msg += f", {failed} 个失败"
        task_manager.update_progress(task_id, 95, msg)
        logger.info(f"[下载] {msg}")

        return {
            "total_markets": len(downloaded_markets),
            "total_snapshots": total_snapshots,
            "skipped": skipped,
            "failed": failed,
            "markets": downloaded_markets,
        }

    def get_resolved_markets(self, page: int = 1, page_size: int = 20) -> dict:
        """从 DB 查询已结算市场列表（含快照数量）。"""
        session: Session = self._db.get_session()
        try:
            from sqlalchemy import func

            # 查询所有非排除的已结算事件
            query = session.query(PolymarketEvent).filter(
                PolymarketEvent.is_excluded == False  # noqa: E712
            ).order_by(PolymarketEvent.volume.desc())

            total = query.count()
            events = query.offset((page - 1) * page_size).limit(page_size).all()

            items = []
            for e in events:
                # 统计快照数
                snap_count = session.query(func.count(PolymarketPriceSnapshot.id)).filter(
                    PolymarketPriceSnapshot.condition_id == e.condition_id
                ).scalar() or 0

                items.append({
                    "condition_id": e.condition_id,
                    "token_id": e.token_id,
                    "question": e.question,
                    "category": e.category,
                    "volume": e.volume,
                    "end_date": e.end_date.isoformat() if e.end_date else None,
                    "snapshot_count": snap_count,
                })

            return {"total": total, "page": page, "page_size": page_size, "items": items}
        finally:
            session.close()

    def get_price_series(self, condition_id: str) -> list[dict]:
        """获取某个市场的历史价格序列（按时间排序）。"""
        session: Session = self._db.get_session()
        try:
            snapshots = session.query(PolymarketPriceSnapshot).filter(
                PolymarketPriceSnapshot.condition_id == condition_id
            ).order_by(PolymarketPriceSnapshot.timestamp.asc()).all()

            return [
                {
                    "timestamp": s.timestamp.isoformat() if s.timestamp else None,
                    "yes_price": s.yes_price,
                    "no_price": s.no_price,
                }
                for s in snapshots
            ]
        finally:
            session.close()
