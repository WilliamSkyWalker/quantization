"""
Polymarket P&L 分析器

在 Polymarket 告警触发后，拉取对应美股 ticker 的实际股价，
模拟建仓并计算持有 N 天的实际收益，汇总胜率/收益率/夏普。

注意：这不是多因子量化回测引擎，而是 Polymarket 事件驱动的 P&L 分析工具。
"""

import json
import logging
import math
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import pytz
from sqlalchemy import text

from services.config import LOG_LEVEL
from services.data.database import DatabaseManager
from services.polymarket.models import PolymarketAlert
from tasks.manager import task_manager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

ET = pytz.timezone("US/Eastern")
MARKET_CLOSE_HOUR = 16  # 4:00 PM ET


class PolymarketPnlAnalyzer:
    """
    Polymarket 事件驱动 P&L 分析器。

    两种运行模式:
    - 模式 A: 从已有回测结果的 alerts JSON 运行
    - 模式 B: 从 DB polymarket_alert 表运行
    """

    def __init__(self):
        self._db = DatabaseManager()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def run_from_alerts(
        self,
        task_id: str,
        alerts: list[dict],
        holding_days: int = 5,
        min_confidence: float = 0.0,
    ) -> dict:
        """模式 A: 从 alerts JSON 列表运行 P&L 回测。"""
        task_manager.update_progress(task_id, 5, "解析告警数据...")
        trades_input = self._extract_trades_from_alerts(alerts, min_confidence)
        return self._run_pnl(task_id, trades_input, holding_days, min_confidence, len(alerts))

    def run_from_db(
        self,
        task_id: str,
        holding_days: int = 5,
        min_confidence: float = 0.0,
        limit: int = 0,
    ) -> dict:
        """模式 B: 从 DB 告警表运行 P&L 回测（同日同市场同类型自动去重）。"""
        task_manager.update_progress(task_id, 5, "从数据库加载告警...")
        self._db.init_tables()
        session = self._db.get_session()
        try:
            query = session.query(PolymarketAlert).order_by(
                PolymarketAlert.created_at.desc()
            )
            if limit and limit > 0:
                query = query.limit(limit)
            db_alerts = query.all()

            # 同日同市场同类型去重（保留最早的一条）
            seen_keys: set[tuple] = set()
            alerts = []
            for a in db_alerts:
                day_key = (
                    a.condition_id,
                    a.alert_type,
                    a.created_at.date() if a.created_at else None,
                )
                if day_key in seen_keys:
                    continue
                seen_keys.add(day_key)

                tickers = []
                if a.affected_tickers:
                    try:
                        tickers = json.loads(a.affected_tickers) if isinstance(a.affected_tickers, str) else a.affected_tickers
                    except (json.JSONDecodeError, TypeError):
                        pass

                alerts.append({
                    "condition_id": a.condition_id,
                    "alert_type": a.alert_type,
                    "question": a.question,
                    "price_before": a.price_before,
                    "price_after": a.price_after,
                    "price_change": a.price_change,
                    "timestamp": a.created_at.isoformat() if a.created_at else None,
                    "affected_tickers": tickers,
                    "llm_confidence": a.llm_confidence,
                    "llm_sentiment": a.llm_sentiment,
                    "llm_summary": a.llm_summary,
                })

            logger.info(f"去重后告警: {len(alerts)} 条 (原始 {len(db_alerts)} 条)")
            trades_input = self._extract_trades_from_alerts(alerts, min_confidence)
            return self._run_pnl(task_id, trades_input, holding_days, min_confidence, len(alerts))
        finally:
            session.close()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _extract_trades_from_alerts(
        self,
        alerts: list[dict],
        min_confidence: float,
    ) -> list[dict]:
        """从告警列表中提取 (ticker, direction, confidence, alert_time, ...) 。"""
        logger.info(f"提取交易信号: {len(alerts)} 条告警, min_confidence={min_confidence}")
        trades = []
        for idx, alert in enumerate(alerts):
            tickers = alert.get("affected_tickers", [])
            if not tickers:
                continue

            alert_time = alert.get("timestamp")
            alert_type = alert.get("alert_type", "unknown")
            question = alert.get("question", "")

            for t in tickers:
                ticker = t.get("ticker", "")
                if not ticker:
                    continue
                direction = t.get("direction", "bullish")
                confidence = t.get("confidence", 0.5)
                if confidence < min_confidence:
                    continue

                trades.append({
                    "alert_idx": idx,
                    "ticker": ticker,
                    "direction": direction,
                    "confidence": confidence,
                    "alert_time": alert_time,
                    "alert_type": alert_type,
                    "event_question": question[:200],
                })
        logger.info(f"过滤后交易信号: {len(trades)} 条 (min_confidence={min_confidence})")
        return trades

    def _run_pnl(
        self,
        task_id: str,
        trades_input: list[dict],
        holding_days: int,
        min_confidence: float,
        total_alerts: int,
    ) -> dict:
        """核心 P&L 计算逻辑。"""
        if not trades_input:
            task_manager.update_progress(task_id, 100, "无可用交易信号")
            return {
                "trades": [],
                "summary": self._empty_summary(total_alerts, holding_days, min_confidence),
                "config": {
                    "holding_days": holding_days,
                    "total_alerts": total_alerts,
                    "min_confidence": min_confidence,
                },
            }

        task_manager.update_progress(task_id, 15, f"提取到 {len(trades_input)} 条交易信号，正在加载股价...")

        # 收集所有 ticker
        all_tickers = list({t["ticker"] for t in trades_input})

        # 确定时间范围
        timestamps = []
        for t in trades_input:
            if t["alert_time"]:
                try:
                    ts = self._parse_timestamp(t["alert_time"])
                    timestamps.append(ts)
                except Exception:
                    pass

        if not timestamps:
            task_manager.update_progress(task_id, 100, "无有效时间戳")
            return {
                "trades": [],
                "summary": self._empty_summary(total_alerts, holding_days, min_confidence),
                "config": {
                    "holding_days": holding_days,
                    "total_alerts": total_alerts,
                    "min_confidence": min_confidence,
                },
            }

        min_date = (min(timestamps) - timedelta(days=5)).date()
        max_date = (max(timestamps) + timedelta(days=holding_days + 10)).date()

        # 从本地 DB 加载股价
        price_data = self._load_prices(task_id, all_tickers, min_date, max_date)
        task_manager.update_progress(task_id, 50, f"已加载 {len(price_data)} 个 ticker 的股价，正在计算 P&L...")

        # 逐笔计算
        results = []
        for i, trade in enumerate(trades_input):
            if i % 50 == 0:
                pct = 50 + int(40 * i / len(trades_input))
                task_manager.update_progress(task_id, pct, f"计算交易 {i+1}/{len(trades_input)}...")

            ticker = trade["ticker"]
            if ticker not in price_data or price_data[ticker].empty:
                continue

            df = price_data[ticker]
            alert_time = trade["alert_time"]
            if not alert_time:
                continue

            try:
                ts = self._parse_timestamp(alert_time)
            except Exception:
                continue

            entry_date = self._find_entry_date(ts, df.index)
            if entry_date is None:
                continue

            exit_date = self._find_exit_date(entry_date, holding_days, df.index)

            # 如果完整持仓期尚未结束，使用最新可用收盘价做 mark-to-market
            is_mtm = False
            if exit_date is None:
                later_days = df.index[df.index > entry_date]
                if later_days.empty:
                    # 入场日就是最后一天 → 使用入场日收盘价（持仓 0 天，收益 0%）
                    exit_date = entry_date
                else:
                    exit_date = later_days[-1]
                is_mtm = True

            entry_price = float(df.loc[entry_date, "Close"])
            exit_price = float(df.loc[exit_date, "Close"])

            if entry_price <= 0 or not math.isfinite(entry_price) or not math.isfinite(exit_price):
                continue

            if trade["direction"] == "bearish":
                ret = (entry_price - exit_price) / entry_price
            else:
                ret = (exit_price - entry_price) / entry_price

            ret_pct = round(ret * 100, 2)

            # Benchmark: 同期买入持有（纯多）
            bm_ret_pct = round((exit_price - entry_price) / entry_price * 100, 2)

            # 计算实际持仓交易日数
            actual_days = len(df.index[(df.index > entry_date) & (df.index <= exit_date)])

            results.append({
                "alert_idx": trade["alert_idx"],
                "ticker": ticker,
                "direction": trade["direction"],
                "confidence": trade["confidence"],
                "alert_time": trade["alert_time"],
                "entry_date": entry_date.strftime("%Y-%m-%d"),
                "entry_price": round(entry_price, 2),
                "exit_date": exit_date.strftime("%Y-%m-%d"),
                "exit_price": round(exit_price, 2),
                "return_pct": ret_pct,
                "benchmark_pct": bm_ret_pct,
                "alpha_pct": round(ret_pct - bm_ret_pct, 2),
                "is_win": ret_pct > 0,
                "holding_days": actual_days,
                "target_holding_days": holding_days,
                "is_mark_to_market": is_mtm,
                "event_question": trade["event_question"],
                "alert_type": trade["alert_type"],
            })

        task_manager.update_progress(task_id, 92, f"汇总统计 ({len(results)} 笔交易)...")
        summary = self._compute_summary(results, total_alerts)

        # 按收益排序，只返回前 500 条给前端（summary 已用全量计算）
        sorted_trades = sorted(results, key=lambda t: abs(t["return_pct"]), reverse=True)
        return {
            "trades": sorted_trades[:500],
            "summary": summary,
            "config": {
                "holding_days": holding_days,
                "total_alerts": total_alerts,
                "min_confidence": min_confidence,
                "total_trades_computed": len(results),
            },
        }

    def _load_prices(
        self,
        task_id: str,
        tickers: list[str],
        start_date,
        end_date,
    ) -> dict:
        """从本地 us_daily_price 表批量加载美股收盘价。"""
        result = {}
        session = self._db.get_session()
        try:
            batch_size = 50
            for i in range(0, len(tickers), batch_size):
                batch = tickers[i:i + batch_size]
                pct = 15 + int(35 * i / len(tickers))
                task_manager.update_progress(task_id, pct, f"加载股价 ({i+1}-{min(i+batch_size, len(tickers))}/{len(tickers)})...")

                placeholders = ",".join([f":t{j}" for j in range(len(batch))])
                params = {f"t{j}": t for j, t in enumerate(batch)}
                params["start"] = start_date
                params["end"] = end_date

                sql = text(
                    f"SELECT ticker, trade_date, `close` "
                    f"FROM us_daily_price "
                    f"WHERE ticker IN ({placeholders}) "
                    f"AND trade_date BETWEEN :start AND :end "
                    f"ORDER BY trade_date"
                )

                rows = session.execute(sql, params).fetchall()

                data_map: dict[str, list] = {}
                for row in rows:
                    data_map.setdefault(row[0], []).append({
                        "trade_date": row[1],
                        "Close": row[2],
                    })

                for ticker, records in data_map.items():
                    df = pd.DataFrame(records)
                    df["trade_date"] = pd.to_datetime(df["trade_date"])
                    df = df.set_index("trade_date").sort_index()
                    result[ticker] = df

        except Exception as e:
            logger.error(f"加载美股股价失败: {e}")
        finally:
            session.close()

        return result

    @staticmethod
    def _parse_timestamp(ts_str: str) -> datetime:
        """解析时间字符串为 UTC datetime。"""
        if isinstance(ts_str, datetime):
            return ts_str

        # 尝试多种格式
        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ]:
            try:
                dt = datetime.strptime(ts_str, fmt)
                return dt
            except ValueError:
                continue

        # ISO 格式带时区
        if "+" in ts_str or ts_str.endswith("Z"):
            ts_str = ts_str.replace("Z", "+00:00")
            return datetime.fromisoformat(ts_str)

        raise ValueError(f"无法解析时间: {ts_str}")

    @staticmethod
    def _find_entry_date(alert_time: datetime, trading_days, max_search: int = 10):
        """
        根据告警时间找到入场日期。

        - 如果在交易日 16:00 ET 之前 → 当天
        - 如果在 16:00 ET 之后或非交易日 → 下一个交易日
        """
        if trading_days.empty:
            return None

        # 转换为 ET
        if alert_time.tzinfo is None:
            alert_et = ET.localize(alert_time)
        else:
            alert_et = alert_time.astimezone(ET)

        alert_date = alert_et.date()

        # 判断是否在盘前/盘中（16:00 ET 前）
        if alert_et.hour < MARKET_CLOSE_HOUR:
            # 当天如果是交易日，使用当天
            target = pd.Timestamp(alert_date)
            if target in trading_days:
                return target
        # 否则找下一个交易日
        search_date = pd.Timestamp(alert_date) + pd.Timedelta(days=1) if alert_et.hour >= MARKET_CLOSE_HOUR else pd.Timestamp(alert_date)
        for _ in range(max_search):
            if search_date in trading_days:
                return search_date
            search_date += pd.Timedelta(days=1)
        return None

    @staticmethod
    def _find_exit_date(entry_date, holding_days: int, trading_days, max_search: int = 20):
        """找到持有 N 个交易日后的出场日期。"""
        count = 0
        current = entry_date + pd.Timedelta(days=1)
        for _ in range(max_search + holding_days):
            if current in trading_days:
                count += 1
                if count >= holding_days:
                    return current
            current += pd.Timedelta(days=1)
        return None

    def _compute_summary(self, trades: list[dict], total_alerts: int) -> dict:
        """计算汇总统计。"""
        total_trades = len(trades)
        if total_trades == 0:
            return self._empty_summary(total_alerts, 0, 0.0)

        mtm_count = sum(1 for t in trades if t.get("is_mark_to_market"))
        settled_trades = [t for t in trades if not t.get("is_mark_to_market")]

        returns = [t["return_pct"] for t in trades]
        wins = [t for t in trades if t["is_win"]]
        losses = [t for t in trades if not t["is_win"]]

        win_count = len(wins)
        loss_count = len(losses)
        win_rate = win_count / total_trades if total_trades > 0 else 0

        avg_return = sum(returns) / total_trades
        sorted_returns = sorted(returns)
        median_return = sorted_returns[total_trades // 2] if total_trades % 2 == 1 else (
            sorted_returns[total_trades // 2 - 1] + sorted_returns[total_trades // 2]
        ) / 2

        total_return = sum(returns)
        max_win = max(returns) if returns else 0
        max_loss = min(returns) if returns else 0

        avg_win = sum(t["return_pct"] for t in wins) / win_count if win_count > 0 else 0
        avg_loss = sum(t["return_pct"] for t in losses) / loss_count if loss_count > 0 else 0

        # Profit factor
        total_gains = sum(t["return_pct"] for t in wins)
        total_losses_abs = abs(sum(t["return_pct"] for t in losses))
        profit_factor = total_gains / total_losses_abs if total_losses_abs > 0 else None if total_gains > 0 else 0

        # Sharpe ratio (假设无风险利率=0，按交易收益率序列)
        sharpe = 0.0
        if len(returns) >= 2:
            import statistics
            clean_returns = [r for r in returns if math.isfinite(r)]
            if len(clean_returns) >= 2:
                std = statistics.stdev(clean_returns)
                if std > 0:
                    clean_avg = sum(clean_returns) / len(clean_returns)
                    sharpe = round((clean_avg / std) * math.sqrt(252), 2)

        # 按方向分组
        by_direction = {}
        for direction in ["bullish", "bearish"]:
            subset = [t for t in trades if t["direction"] == direction]
            if subset:
                d_wins = [t for t in subset if t["is_win"]]
                by_direction[direction] = {
                    "count": len(subset),
                    "win_rate": round(len(d_wins) / len(subset), 2),
                    "avg_return": round(sum(t["return_pct"] for t in subset) / len(subset), 2),
                }

        # 按告警类型分组
        by_alert_type = {}
        alert_types = set(t["alert_type"] for t in trades)
        for at in alert_types:
            subset = [t for t in trades if t["alert_type"] == at]
            at_wins = [t for t in subset if t["is_win"]]
            by_alert_type[at] = {
                "count": len(subset),
                "win_rate": round(len(at_wins) / len(subset), 2),
                "avg_return": round(sum(t["return_pct"] for t in subset) / len(subset), 2),
            }

        # 按置信度分组
        by_confidence = {}
        tiers = [
            ("high (>=0.7)", 0.7, 1.01),
            ("medium (0.4-0.7)", 0.4, 0.7),
            ("low (<0.4)", 0.0, 0.4),
        ]
        for label, lo, hi in tiers:
            subset = [t for t in trades if lo <= t["confidence"] < hi]
            if subset:
                c_wins = [t for t in subset if t["is_win"]]
                by_confidence[label] = {
                    "count": len(subset),
                    "win_rate": round(len(c_wins) / len(subset), 2),
                    "avg_return": round(sum(t["return_pct"] for t in subset) / len(subset), 2),
                }

        # Top winners / losers
        sorted_by_return = sorted(trades, key=lambda t: t["return_pct"], reverse=True)
        top_winners = sorted_by_return[:10]
        top_losers = sorted_by_return[-10:][::-1]  # 最差的 10 个，从最差开始

        # 按 ticker 汇总
        ticker_stats_map: dict[str, list] = {}
        for t in trades:
            ticker_stats_map.setdefault(t["ticker"], []).append(t)
        ticker_stats = []
        for ticker, ts in ticker_stats_map.items():
            t_wins = [t for t in ts if t["is_win"]]
            ticker_stats.append({
                "ticker": ticker,
                "count": len(ts),
                "win_rate": round(len(t_wins) / len(ts), 2),
                "avg_return": round(sum(t["return_pct"] for t in ts) / len(ts), 2),
            })
        ticker_stats.sort(key=lambda x: x["count"], reverse=True)

        # Benchmark: 同期买入持有
        bm_returns = [t["benchmark_pct"] for t in trades if "benchmark_pct" in t]
        alpha_returns = [t["alpha_pct"] for t in trades if "alpha_pct" in t]
        avg_benchmark = sum(bm_returns) / len(bm_returns) if bm_returns else 0
        avg_alpha = sum(alpha_returns) / len(alpha_returns) if alpha_returns else 0
        bm_wins = sum(1 for r in bm_returns if r > 0)
        bm_win_rate = bm_wins / len(bm_returns) if bm_returns else 0

        return {
            "total_trades": total_trades,
            "settled_trades": len(settled_trades),
            "mtm_trades": mtm_count,
            "valid_trades": total_trades,
            "skipped_trades": 0,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": round(win_rate, 2),
            "avg_return_pct": round(avg_return, 2),
            "median_return_pct": round(median_return, 2),
            "total_return_pct": round(total_return, 2),
            "sharpe_ratio": sharpe,
            "max_single_win_pct": round(max_win, 2),
            "max_single_loss_pct": round(max_loss, 2),
            "avg_win_return_pct": round(avg_win, 2),
            "avg_loss_return_pct": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
            "benchmark_avg_pct": round(avg_benchmark, 2),
            "benchmark_win_rate": round(bm_win_rate, 2),
            "alpha_avg_pct": round(avg_alpha, 2),
            "by_direction": by_direction,
            "by_alert_type": by_alert_type,
            "by_confidence_tier": by_confidence,
            "top_winners": top_winners,
            "top_losers": top_losers,
            "ticker_stats": ticker_stats[:20],
        }

    @staticmethod
    def _empty_summary(total_alerts: int, holding_days: int, min_confidence: float) -> dict:
        return {
            "total_trades": 0,
            "valid_trades": 0,
            "skipped_trades": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate": 0,
            "avg_return_pct": 0,
            "median_return_pct": 0,
            "total_return_pct": 0,
            "sharpe_ratio": 0,
            "max_single_win_pct": 0,
            "max_single_loss_pct": 0,
            "avg_win_return_pct": 0,
            "avg_loss_return_pct": 0,
            "profit_factor": 0,
            "benchmark_avg_pct": 0,
            "benchmark_win_rate": 0,
            "alpha_avg_pct": 0,
            "by_direction": {},
            "by_alert_type": {},
            "by_confidence_tier": {},
            "top_winners": [],
            "top_losers": [],
            "ticker_stats": [],
        }
