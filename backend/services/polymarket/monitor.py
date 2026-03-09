"""
Polymarket 监控服务

- 定期从 Gamma API 发现高交易量预测市场
- 通过 CLOB WebSocket 接收实时赔率流
- 检测赔率 Spike（5min / 1h / 24h 三档）
- 触发告警交给 AlertManager 处理
"""

import asyncio
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

import requests
from sqlalchemy.orm import Session

from backend.services.config import (
    POLYMARKET_GAMMA_API,
    POLYMARKET_CLOB_WS,
    POLYMARKET_SPIKE_5M,
    POLYMARKET_SPIKE_1H,
    POLYMARKET_SPIKE_24H,
    POLYMARKET_MIN_VOLUME,
    POLYMARKET_MAX_MARKETS,
    POLYMARKET_SNAPSHOT_INTERVAL,
    POLYMARKET_DISCOVERY_INTERVAL,
    LOG_LEVEL,
)
from backend.services.data.database import DatabaseManager
from backend.services.polymarket.models import PolymarketEvent, PolymarketPriceSnapshot
from backend.services.polymarket.utils import category_from_tags
from backend.tasks.manager import task_manager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# Spike 检测配置：(alert_type, 回看秒数, 阈值)
SPIKE_RULES = [
    ("spike_5m", 300, POLYMARKET_SPIKE_5M),
    ("spike_1h", 3600, POLYMARKET_SPIKE_1H),
    ("spike_24h", 86400, POLYMARKET_SPIKE_24H),
]


class PriceHistory:
    """滚动价格历史，保留最近 24h 数据用于 Spike 检测。"""

    def __init__(self, max_age_seconds: int = 86400):
        self._data: deque[tuple[float, float]] = deque()  # (timestamp, price)
        self._max_age = max_age_seconds

    def add(self, price: float, ts: Optional[float] = None):
        now = ts or time.time()
        self._data.append((now, price))
        self._trim(now)

    def get_price_at(self, seconds_ago: int) -> Optional[float]:
        """获取 N 秒前的价格（最近邻匹配）。"""
        if not self._data:
            return None
        target = time.time() - seconds_ago
        best = None
        best_diff = float("inf")
        for ts, price in self._data:
            diff = abs(ts - target)
            if diff < best_diff:
                best_diff = diff
                best = price
        # 如果最近邻距离超过窗口 20%，认为数据不足
        if best_diff > seconds_ago * 0.2:
            return None
        return best

    @property
    def latest(self) -> Optional[float]:
        return self._data[-1][1] if self._data else None

    def _trim(self, now: float):
        cutoff = now - self._max_age
        while self._data and self._data[0][0] < cutoff:
            self._data.popleft()


class PolymarketMonitor:
    """
    Polymarket 预测市场监控器。

    通过 TaskManager 提交为后台任务运行，主循环：
    1. 定期调用 Gamma API 发现市场
    2. 启动 asyncio 线程连接 CLOB WebSocket
    3. 检测价格 Spike 并触发告警
    """

    def __init__(self):
        self._stop_event = threading.Event()
        self._stop_event.set()  # 初始为「已停止」状态
        self._markets: dict[str, dict] = {}  # condition_id -> market info
        self._price_histories: dict[str, PriceHistory] = {}  # condition_id -> PriceHistory
        self._ws_thread: Optional[threading.Thread] = None
        self._alert_manager = None  # lazy init to avoid circular import
        self._db = DatabaseManager()

    def start(self, task_id: str):
        """阻塞式运行监控，由 TaskManager 在线程池中调用。"""
        self._stop_event.clear()
        self._db.init_tables()
        logger.info("Polymarket 监控启动")
        task_manager.update_progress(task_id, 10, "正在发现市场...")

        last_discovery = 0
        last_snapshot = 0

        while not self._stop_event.is_set():
            now = time.time()

            # 定期发现市场
            if now - last_discovery >= POLYMARKET_DISCOVERY_INTERVAL or last_discovery == 0:
                try:
                    self._discover_markets()
                    last_discovery = now
                    task_manager.update_progress(
                        task_id, 30,
                        f"监控中: {len(self._markets)} 个市场"
                    )
                except Exception as e:
                    logger.error(f"市场发现失败: {e}")

                # 启动/重启 WebSocket 线程
                if self._markets and (self._ws_thread is None or not self._ws_thread.is_alive()):
                    self._start_ws_thread()

            # 定期保存快照
            if now - last_snapshot >= POLYMARKET_SNAPSHOT_INTERVAL:
                try:
                    self._save_snapshots()
                    last_snapshot = now
                except Exception as e:
                    logger.error(f"快照保存失败: {e}")

            self._stop_event.wait(5)

        logger.info("Polymarket 监控已停止")

    def stop(self):
        """信号停止监控。"""
        self._stop_event.set()

    @property
    def is_running(self) -> bool:
        return not self._stop_event.is_set()

    @property
    def monitored_markets(self) -> list[dict]:
        """返回当前监控中的市场信息。"""
        result = []
        for cid, info in self._markets.items():
            history = self._price_histories.get(cid)
            result.append({
                "condition_id": cid,
                "question": info.get("question", ""),
                "category": info.get("category", ""),
                "yes_price": history.latest if history else info.get("yes_price"),
                "volume": info.get("volume", 0),
            })
        return result

    def _discover_markets(self):
        """从 Gamma API 发现高交易量市场。"""
        url = f"{POLYMARKET_GAMMA_API}/events"
        params = {
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
            "limit": POLYMARKET_MAX_MARKETS,
        }

        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        events = resp.json()

        new_count = 0
        session: Session = self._db.get_session()
        try:
            for event in events:
                markets = event.get("markets", [])
                for market in markets:
                    volume = float(market.get("volume", 0) or 0)
                    if volume < POLYMARKET_MIN_VOLUME:
                        continue

                    condition_id = market.get("conditionId") or market.get("condition_id", "")
                    if not condition_id:
                        continue

                    try:
                        prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                        yes_price = float(prices[0])
                    except (json.JSONDecodeError, ValueError, IndexError, TypeError):
                        yes_price = 0.5
                    no_price = 1.0 - yes_price

                    category = category_from_tags(event.get("tags", []))
                    market_info = {
                        "condition_id": condition_id,
                        "token_id": market.get("clobTokenIds", "").strip("[]").split(",")[0].strip('" '),
                        "question": market.get("question", event.get("title", "")),
                        "description": market.get("description", event.get("description", "")),
                        "category": category,
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "volume": volume,
                        "liquidity": float(market.get("liquidity", 0) or 0),
                        "slug": event.get("slug", ""),
                        "gamma_market_id": market.get("id", ""),
                        "end_date": market.get("endDate"),
                    }

                    self._markets[condition_id] = market_info

                    if condition_id not in self._price_histories:
                        self._price_histories[condition_id] = PriceHistory()
                        self._price_histories[condition_id].add(yes_price)

                    # Upsert to DB
                    existing = session.query(PolymarketEvent).filter_by(
                        condition_id=condition_id
                    ).first()
                    if existing:
                        existing.outcome_yes_price = yes_price
                        existing.outcome_no_price = no_price
                        existing.volume = volume
                        existing.liquidity = market_info["liquidity"]
                        existing.is_active = True
                        if category and not existing.category:
                            existing.category = category
                    else:
                        end_date = None
                        if market_info["end_date"]:
                            try:
                                end_date = datetime.fromisoformat(
                                    str(market_info["end_date"]).replace("Z", "+00:00")
                                )
                            except (ValueError, TypeError):
                                pass
                        event_obj = PolymarketEvent(
                            condition_id=condition_id,
                            token_id=market_info["token_id"],
                            question=market_info["question"][:1000],
                            description=(market_info["description"] or "")[:5000],
                            category=market_info["category"],
                            outcome_yes_price=yes_price,
                            outcome_no_price=no_price,
                            volume=volume,
                            liquidity=market_info["liquidity"],
                            end_date=end_date,
                            is_active=True,
                            slug=market_info["slug"],
                            gamma_market_id=market_info["gamma_market_id"],
                        )
                        session.add(event_obj)
                        new_count += 1

            session.commit()
            logger.info(
                f"市场发现完成: 共 {len(self._markets)} 个市场, "
                f"新增 {new_count} 个"
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _start_ws_thread(self):
        """启动独立线程运行 asyncio WebSocket。"""
        self._ws_thread = threading.Thread(
            target=self._run_ws_loop,
            daemon=True,
            name="polymarket-ws",
        )
        self._ws_thread.start()
        logger.info("WebSocket 线程已启动")

    def _run_ws_loop(self):
        """在独立线程中运行 asyncio event loop。"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_connect())
        except Exception as e:
            logger.error(f"WebSocket 循环异常退出: {e}")
        finally:
            loop.close()

    async def _ws_connect(self):
        """连接 CLOB WebSocket 并订阅市场。"""
        try:
            import websockets
        except ImportError:
            logger.warning("websockets 库未安装，跳过实时赔率流")
            return

        while not self._stop_event.is_set():
            try:
                async with websockets.connect(POLYMARKET_CLOB_WS) as ws:
                    logger.info("CLOB WebSocket 已连接")

                    # 订阅所有监控中的市场
                    token_ids = []
                    for info in self._markets.values():
                        tid = info.get("token_id", "")
                        if tid:
                            token_ids.append(tid)

                    if token_ids:
                        # 分批订阅，每批最多 100 个，避免消息体超过 WebSocket 帧限制
                        batch_size = 100
                        for i in range(0, len(token_ids), batch_size):
                            batch = token_ids[i:i + batch_size]
                            subscribe_msg = {
                                "type": "market",
                                "assets_ids": batch,
                            }
                            await ws.send(json.dumps(subscribe_msg))
                        logger.info(f"已订阅 {len(token_ids)} 个 token（{(len(token_ids) - 1) // batch_size + 1} 批）")

                    async for message in ws:
                        if self._stop_event.is_set():
                            break
                        try:
                            self._handle_ws_message(json.loads(message))
                        except Exception as e:
                            logger.debug(f"WS 消息处理异常: {e}")

            except Exception as e:
                if self._stop_event.is_set():
                    break
                logger.warning(f"WebSocket 断开: {e}, 5s 后重连")
                await asyncio.sleep(5)

    def _handle_ws_message(self, data: dict):
        """处理 WebSocket 消息：更新价格历史 + 检测 Spike。"""
        # CLOB WS 消息格式: {"asset_id": ..., "price": ..., ...}
        asset_id = data.get("asset_id", "")
        price = data.get("price")
        if price is None:
            return

        price = float(price)

        # 找到对应的 condition_id
        condition_id = None
        for cid, info in self._markets.items():
            if info.get("token_id") == asset_id:
                condition_id = cid
                break

        if not condition_id:
            return

        # 更新价格历史
        history = self._price_histories.setdefault(condition_id, PriceHistory())
        history.add(price)

        # 更新内存中的 yes_price
        self._markets[condition_id]["yes_price"] = price
        self._markets[condition_id]["no_price"] = 1.0 - price

        # 推送实时价格到前端 WebSocket
        self._push_price_update(condition_id, price)

        # 检测 Spike
        self._check_spikes(condition_id, price)

    def _check_spikes(self, condition_id: str, current_price: float):
        """检查三档时间窗口是否有 Spike，仅触发最显著的一档。"""
        history = self._price_histories.get(condition_id)
        if not history:
            return

        best_spike = None
        best_abs_change = 0.0

        for alert_type, lookback_seconds, threshold in SPIKE_RULES:
            old_price = history.get_price_at(lookback_seconds)
            if old_price is None:
                continue

            change = current_price - old_price
            if abs(change) >= threshold and abs(change) > best_abs_change:
                best_abs_change = abs(change)
                best_spike = (alert_type, old_price, lookback_seconds)

        if best_spike:
            alert_type, price_before, timeframe_seconds = best_spike
            self._trigger_alert(
                condition_id=condition_id,
                alert_type=alert_type,
                price_before=price_before,
                price_after=current_price,
                timeframe_seconds=timeframe_seconds,
            )

    def _trigger_alert(self, condition_id: str, alert_type: str,
                       price_before: float, price_after: float,
                       timeframe_seconds: int):
        """交给 AlertManager 处理告警。"""
        if self._alert_manager is None:
            from backend.services.polymarket.alert_manager import AlertManager
            self._alert_manager = AlertManager()

        market_info = self._markets.get(condition_id, {})
        self._alert_manager.trigger_alert(
            condition_id=condition_id,
            alert_type=alert_type,
            market_info=market_info,
            price_before=price_before,
            price_after=price_after,
            timeframe_seconds=timeframe_seconds,
        )

    def _save_snapshots(self):
        """将当前价格快照写入 DB。"""
        session: Session = self._db.get_session()
        now = datetime.now()
        count = 0
        try:
            for cid, history in self._price_histories.items():
                price = history.latest
                if price is None:
                    continue
                market = self._markets.get(cid, {})
                snapshot = PolymarketPriceSnapshot(
                    condition_id=cid,
                    timestamp=now,
                    yes_price=price,
                    no_price=1.0 - price,
                    spread=0.0,
                    volume_24h=market.get("volume", 0),
                    source="websocket",
                )
                session.add(snapshot)
                count += 1
            session.commit()
            logger.debug(f"保存 {count} 条快照")
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _push_price_update(self, condition_id: str, price: float):
        """通过 Django Channels 推送实时价格。"""
        try:
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync

            channel_layer = get_channel_layer()
            if channel_layer:
                async_to_sync(channel_layer.group_send)(
                    "polymarket",
                    {
                        "type": "price_update",
                        "data": {
                            "condition_id": condition_id,
                            "yes_price": price,
                            "no_price": 1.0 - price,
                            "timestamp": datetime.now().isoformat(),
                        },
                    },
                )
        except Exception:
            pass


# 全局单例
_monitor: Optional[PolymarketMonitor] = None
_monitor_lock = threading.Lock()


def get_monitor() -> PolymarketMonitor:
    """获取全局 PolymarketMonitor 单例。"""
    global _monitor
    with _monitor_lock:
        if _monitor is None:
            _monitor = PolymarketMonitor()
        return _monitor
