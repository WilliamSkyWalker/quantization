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

from backend.services.config import POLYMARKET_GAMMA_API, POLYMARKET_MIN_VOLUME, LOG_LEVEL
from backend.services.data.database import DatabaseManager
from backend.services.polymarket.models import PolymarketEvent, PolymarketPriceSnapshot
from backend.tasks.manager import task_manager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

CLOB_BASE = "https://clob.polymarket.com"
# CLOB API rate limit: 9000 req/10s, we stay conservative
CLOB_REQUEST_INTERVAL = 0.05  # 50ms between requests


class PolymarketHistoryDownloader:
    """下载 Polymarket 已结算市场的历史赔率数据。"""

    def __init__(self):
        self._db = DatabaseManager()

    def discover_resolved_markets(
        self,
        task_id: str,
        limit: int = 50,
        min_volume: int = 0,
    ) -> list[dict]:
        """
        从 Gamma API 发现已结算的高交易量市场。

        Returns:
            已结算市场列表 [{condition_id, token_id, question, ...}]
        """
        self._db.init_tables()
        task_manager.update_progress(task_id, 5, "正在从 Gamma API 获取已结算市场...")

        url = f"{POLYMARKET_GAMMA_API}/events"
        params = {
            "closed": "true",
            "order": "volume",
            "ascending": "false",
            "limit": limit,
        }

        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        events = resp.json()

        vol_threshold = min_volume or POLYMARKET_MIN_VOLUME
        results = []
        session: Session = self._db.get_session()

        try:
            for event in events:
                for market in event.get("markets", []):
                    volume = float(market.get("volume", 0) or 0)
                    if volume < vol_threshold:
                        continue

                    condition_id = market.get("conditionId") or market.get("condition_id", "")
                    if not condition_id:
                        continue

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
                        import json as _json
                        prices = _json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                        yes_price = float(prices[0])
                    except (ValueError, IndexError, TypeError):
                        yes_price = 0.5

                    market_info = {
                        "condition_id": condition_id,
                        "token_id": token_id,
                        "question": market.get("question", event.get("title", "")),
                        "description": market.get("description", event.get("description", "")),
                        "category": event.get("category", ""),
                        "yes_price": yes_price,
                        "volume": volume,
                        "liquidity": float(market.get("liquidity", 0) or 0),
                        "slug": event.get("slug", ""),
                        "gamma_market_id": market.get("id", ""),
                        "end_date": market.get("endDate"),
                        "resolved": True,
                    }
                    results.append(market_info)

                    # Upsert to DB
                    existing = session.query(PolymarketEvent).filter_by(
                        condition_id=condition_id
                    ).first()
                    if existing:
                        existing.volume = volume
                        existing.is_active = False
                    else:
                        end_date = None
                        if market_info["end_date"]:
                            try:
                                end_date = datetime.fromisoformat(
                                    str(market_info["end_date"]).replace("Z", "+00:00")
                                )
                            except (ValueError, TypeError):
                                pass

                        session.add(PolymarketEvent(
                            condition_id=condition_id,
                            token_id=token_id,
                            question=market_info["question"][:1000],
                            description=(market_info["description"] or "")[:5000],
                            category=market_info["category"],
                            outcome_yes_price=yes_price,
                            outcome_no_price=1.0 - yes_price,
                            volume=volume,
                            liquidity=market_info["liquidity"],
                            end_date=end_date,
                            is_active=False,
                            slug=market_info["slug"],
                            gamma_market_id=market_info["gamma_market_id"],
                        ))

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        task_manager.update_progress(
            task_id, 20,
            f"发现 {len(results)} 个已结算市场"
        )
        logger.info(f"已结算市场发现完成: {len(results)} 个")
        return results

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

        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        history = data.get("history", [])
        if not history:
            # 已结算市场在细粒度下可能返回空数据，尝试更粗粒度（12h）
            if fidelity < 720:
                logger.info(f"token {token_id} 在 {fidelity}min 粒度下无数据，尝试 720min")
                params["fidelity"] = 720
                resp2 = requests.get(url, params=params, timeout=30)
                resp2.raise_for_status()
                data = resp2.json()
                history = data.get("history", [])

        if not history:
            logger.warning(f"token {token_id} 无历史数据")
            return []

        # 存入 DB
        session: Session = self._db.get_session()
        saved = 0
        try:
            for point in history:
                ts = point.get("t", 0)
                price = float(point.get("p", 0))
                if ts == 0 or price == 0:
                    continue

                snapshot = PolymarketPriceSnapshot(
                    condition_id=condition_id,
                    timestamp=datetime.fromtimestamp(ts),
                    yes_price=price,
                    no_price=round(1.0 - price, 4),
                    spread=0.0,
                    volume_24h=0,
                    source="clob_history",
                )
                session.add(snapshot)
                saved += 1

            session.commit()
            logger.info(f"保存 {saved} 条历史快照: {condition_id}")
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        return history

    def download_batch(
        self,
        task_id: str,
        markets: Optional[list[dict]] = None,
        limit: int = 20,
        fidelity: int = 60,
    ):
        """
        批量下载已结算市场历史数据。

        完整流程：
        1. 发现已结算市场（如未提供 markets）
        2. 逐个下载历史价格
        """
        self._db.init_tables()

        if markets is None:
            markets = self.discover_resolved_markets(task_id, limit=limit)

        if not markets:
            task_manager.update_progress(task_id, 100, "未发现符合条件的已结算市场")
            return {"total_markets": 0, "total_snapshots": 0}

        total = len(markets)
        total_snapshots = 0
        downloaded_markets = []

        for i, market in enumerate(markets):
            token_id = market.get("token_id", "")
            condition_id = market.get("condition_id", "")
            if not token_id:
                continue

            progress = 20 + int(70 * (i / total))
            task_manager.update_progress(
                task_id, progress,
                f"下载历史数据 ({i + 1}/{total}): {market.get('question', '')[:40]}..."
            )

            try:
                history = self.download_price_history(
                    task_id=task_id,
                    token_id=token_id,
                    condition_id=condition_id,
                    fidelity=fidelity,
                )
                count = len(history)
                total_snapshots += count
                downloaded_markets.append({
                    "condition_id": condition_id,
                    "question": market.get("question", ""),
                    "data_points": count,
                })
                logger.info(f"[{i + 1}/{total}] {condition_id}: {count} 条数据")
            except Exception as e:
                logger.warning(f"下载失败 {condition_id}: {e}")

            time.sleep(CLOB_REQUEST_INTERVAL)

        task_manager.update_progress(
            task_id, 95,
            f"下载完成: {len(downloaded_markets)}/{total} 个市场, {total_snapshots} 条数据"
        )

        return {
            "total_markets": len(downloaded_markets),
            "total_snapshots": total_snapshots,
            "markets": downloaded_markets,
        }

    def get_resolved_markets(self, page: int = 1, page_size: int = 20) -> dict:
        """从 DB 查询已结算市场列表（含快照数量）。"""
        session: Session = self._db.get_session()
        try:
            from sqlalchemy import func

            # 查询所有非活跃（已结算）事件
            query = session.query(PolymarketEvent).filter(
                PolymarketEvent.is_active == False  # noqa: E712
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
