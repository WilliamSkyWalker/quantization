"""
Polymarket 告警管理器

- 接收 Spike 检测触发的告警
- 去重（同一事件+类型在冷却期内不重复触发）
- 调用 EventAnalyzer 获取 LLM 分析
- 持久化到 polymarket_alert 表
- 通过 Django Channels 推送到前端
"""

import json
import logging
import time
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from services.config import POLYMARKET_LLM_COOLDOWN, LOG_LEVEL
# DatabaseManager 已废弃
from sentiment.services.polymarket.models import PolymarketAlert
from sentiment.services.polymarket.event_analyzer import EventAnalyzer

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class AlertManager:
    """告警管理器：去重、LLM 分析、持久化、WebSocket 推送。"""

    def __init__(self):
        self._db = None  # DatabaseManager 已废弃
        self._analyzer = EventAnalyzer()
        # 去重缓存: event_slug (或 condition_id) -> last_trigger_timestamp
        # 同一事件的所有 market 共享冷却，不再按 alert_type 区分
        self._cooldown_cache: dict[str, float] = {}

    def trigger_alert(
        self,
        condition_id: str,
        alert_type: str,
        market_info: dict,
        price_before: float,
        price_after: float,
        timeframe_seconds: int,
    ):
        """
        处理一条告警。

        1. 去重检查
        2. LLM 分析
        3. 写入 DB
        4. WebSocket 推送
        """
        # 去重：按事件 slug 聚合（同一事件的所有 market + 所有 alert_type 共享冷却）
        event_slug = market_info.get("slug", "")
        cache_key = event_slug if event_slug else condition_id
        now = time.time()
        last_trigger = self._cooldown_cache.get(cache_key, 0)
        if now - last_trigger < POLYMARKET_LLM_COOLDOWN:
            logger.debug(
                f"告警冷却中 (event={cache_key[:40]}): {condition_id} {alert_type}, "
                f"距上次 {now - last_trigger:.0f}s < {POLYMARKET_LLM_COOLDOWN}s"
            )
            return
        self._cooldown_cache[cache_key] = now

        price_change = price_after - price_before
        question = market_info.get("question", "")

        logger.info(
            f"告警触发: {alert_type} | {question[:60]} | "
            f"{price_before:.2%} → {price_after:.2%} ({price_change:+.2%})"
        )

        # LLM 分析
        llm_result = None
        if self._analyzer.is_available():
            llm_result = self._analyzer.analyze({
                "question": question,
                "description": market_info.get("description", ""),
                "category": market_info.get("category", ""),
                "price_before": price_before,
                "price_after": price_after,
                "price_change": price_change,
                "alert_type": alert_type,
                "timeframe_seconds": timeframe_seconds,
            })

        # 构建告警记录
        def _json_or_none(key):
            if llm_result and llm_result.get(key):
                return json.dumps(llm_result[key], ensure_ascii=False)
            logger.debug(f"trigger_alert: LLM 结果中 {key} 为空，返回 None")
            return None

        alert = PolymarketAlert(
            condition_id=condition_id,
            alert_type=alert_type,
            price_before=round(price_before, 4),
            price_after=round(price_after, 4),
            price_change=round(price_change, 4),
            timeframe_seconds=timeframe_seconds,
            question=question[:1000],
            affected_tickers=_json_or_none("affected_tickers"),
            affected_a_shares=_json_or_none("affected_a_shares"),
            affected_sectors=_json_or_none("affected_sectors"),
            affected_sw_industries=_json_or_none("affected_sw_industries"),
            llm_summary=llm_result["summary"] if llm_result else None,
            llm_sentiment=llm_result["overall_sentiment"] if llm_result else None,
            llm_confidence=llm_result["confidence"] if llm_result else None,
            is_read=False,
        )

        # 写入 DB
        session: Session = self._db.get_session()
        try:
            session.add(alert)
            session.commit()
            alert_id = alert.id
            logger.info(f"告警已保存: id={alert_id}")
        except Exception:
            session.rollback()
            logger.error("告警保存失败", exc_info=True)
            return
        finally:
            session.close()

        # WebSocket 推送
        alert_data = {
            "id": alert_id,
            "condition_id": condition_id,
            "alert_type": alert_type,
            "price_before": round(price_before, 4),
            "price_after": round(price_after, 4),
            "price_change": round(price_change, 4),
            "timeframe_seconds": timeframe_seconds,
            "question": question,
            "affected_tickers": llm_result["affected_tickers"] if llm_result else [],
            "affected_a_shares": llm_result.get("affected_a_shares", []) if llm_result else [],
            "affected_sectors": llm_result["affected_sectors"] if llm_result else [],
            "affected_sw_industries": llm_result.get("affected_sw_industries", []) if llm_result else [],
            "llm_summary": llm_result["summary"] if llm_result else None,
            "llm_sentiment": llm_result["overall_sentiment"] if llm_result else None,
            "llm_confidence": llm_result["confidence"] if llm_result else None,
            "created_at": datetime.now().isoformat(),
        }
        self._push_alert(alert_data)

    def _push_alert(self, alert_data: dict):
        """通过 Django Channels 推送告警。"""
        try:
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync

            channel_layer = get_channel_layer()
            if channel_layer:
                async_to_sync(channel_layer.group_send)(
                    "polymarket",
                    {
                        "type": "alert",
                        "data": alert_data,
                    },
                )
        except Exception as e:
            logger.debug(f"WebSocket 推送失败: {e}")

    def get_alerts(
        self,
        page: int = 1,
        page_size: int = 20,
        is_read: Optional[bool] = None,
    ) -> dict:
        """查询告警列表（分页）。"""
        session: Session = self._db.get_session()
        try:
            query = session.query(PolymarketAlert).order_by(
                PolymarketAlert.created_at.desc()
            )
            if is_read is not None:
                query = query.filter(PolymarketAlert.is_read == is_read)

            total = query.count()
            alerts = query.offset((page - 1) * page_size).limit(page_size).all()

            items = []
            for a in alerts:
                items.append({
                    "id": a.id,
                    "condition_id": a.condition_id,
                    "alert_type": a.alert_type,
                    "price_before": a.price_before,
                    "price_after": a.price_after,
                    "price_change": a.price_change,
                    "timeframe_seconds": a.timeframe_seconds,
                    "question": a.question,
                    "affected_tickers": json.loads(a.affected_tickers) if a.affected_tickers else [],
                    "affected_a_shares": json.loads(a.affected_a_shares) if a.affected_a_shares else [],
                    "affected_sectors": json.loads(a.affected_sectors) if a.affected_sectors else [],
                    "affected_sw_industries": json.loads(a.affected_sw_industries) if a.affected_sw_industries else [],
                    "llm_summary": a.llm_summary,
                    "llm_sentiment": a.llm_sentiment,
                    "llm_confidence": a.llm_confidence,
                    "is_read": a.is_read,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                })

            return {
                "total": total,
                "page": page,
                "page_size": page_size,
                "items": items,
            }
        finally:
            session.close()

    def mark_read(self, alert_id: int) -> bool:
        """标记告警已读。"""
        session: Session = self._db.get_session()
        try:
            alert = session.query(PolymarketAlert).filter_by(id=alert_id).first()
            if not alert:
                logger.debug(f"mark_read: 告警 id={alert_id} 不存在")
                return False
            alert.is_read = True
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.warning(f"mark_read: 标记告警 id={alert_id} 已读失败: {e}")
            return False
        finally:
            session.close()
