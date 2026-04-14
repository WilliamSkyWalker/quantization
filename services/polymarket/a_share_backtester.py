"""
A股 P&L 回测引擎

在 Polymarket 告警触发后，从本地 daily_price 表拉取受影响 A 股的收盘价，
模拟建仓并计算持有 N 天的实际收益，汇总胜率/收益率/夏普。
"""

import json
import logging
import math
from datetime import datetime, date, timedelta
from typing import Optional

import pandas as pd
from sqlalchemy import text

from services.config import LOG_LEVEL
from services.data.database import DatabaseManager
from services.polymarket.models import PolymarketAlert
from tasks.manager import task_manager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# A股交易时间: 9:30-15:00 CST
MARKET_CLOSE_HOUR = 15


class AShareBacktester:
    """
    A股 P&L 回测引擎。

    从 polymarket_alert 表读取 affected_a_shares，
    从 daily_price 表拉取 A 股收盘价，计算持有 N 天的 P&L。
    """

    def __init__(self):
        self._db = DatabaseManager()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def run_from_db(
        self,
        task_id: str,
        holding_days: int = 5,
        min_confidence: float = 0.0,
        limit: int = 0,
    ) -> dict:
        """从 DB 告警表运行 A 股 P&L 回测（同日同市场同类型自动去重）。"""
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
                    logger.debug(f"run_from_db: 告警去重跳过 condition_id={a.condition_id} type={a.alert_type}")
                    continue
                seen_keys.add(day_key)

                a_shares = []
                if a.affected_a_shares:
                    try:
                        a_shares = json.loads(a.affected_a_shares) if isinstance(a.affected_a_shares, str) else a.affected_a_shares
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.warning(f"run_from_db: 解析 affected_a_shares 失败 condition_id={a.condition_id}: {e}")

                if not a_shares:
                    logger.debug(f"run_from_db: condition_id={a.condition_id} 无受影响 A 股，跳过")
                    continue

                alerts.append({
                    "condition_id": a.condition_id,
                    "alert_type": a.alert_type,
                    "question": a.question,
                    "price_before": a.price_before,
                    "price_after": a.price_after,
                    "price_change": a.price_change,
                    "timestamp": a.created_at.isoformat() if a.created_at else None,
                    "affected_a_shares": a_shares,
                    "llm_confidence": a.llm_confidence,
                    "llm_sentiment": a.llm_sentiment,
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
        """从告警列表中提取 (ts_code, name, direction, confidence, ...) 。"""
        trades = []
        for idx, alert in enumerate(alerts):
            a_shares = alert.get("affected_a_shares", [])
            if not a_shares:
                logger.debug(f"_extract_trades_from_alerts: 告警 idx={idx} 无 affected_a_shares，跳过")
                continue

            alert_time = alert.get("timestamp")
            alert_type = alert.get("alert_type", "unknown")
            question = alert.get("question", "")

            for s in a_shares:
                code = s.get("code", "")
                if not code:
                    logger.debug("_extract_trades_from_alerts: A 股 code 为空，跳过")
                    continue
                direction = s.get("direction", "bullish")
                confidence = s.get("confidence", 0.5)
                if confidence < min_confidence:
                    logger.debug(f"_extract_trades_from_alerts: code={code} confidence={confidence} < min_confidence={min_confidence}，跳过")
                    continue

                trades.append({
                    "alert_idx": idx,
                    "ticker": code,
                    "name": s.get("name", code),
                    "direction": direction,
                    "confidence": confidence,
                    "alert_time": alert_time,
                    "alert_type": alert_type,
                    "event_question": question[:200],
                })
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
            logger.debug("_run_pnl: 无可用 A 股交易信号，返回空结果")
            task_manager.update_progress(task_id, 100, "无可用 A 股交易信号")
            return {
                "trades": [],
                "summary": self._empty_summary(),
                "config": {
                    "holding_days": holding_days,
                    "total_alerts": total_alerts,
                    "min_confidence": min_confidence,
                },
            }

        task_manager.update_progress(task_id, 15, f"提取到 {len(trades_input)} 条 A 股交易信号，正在查询股价...")

        # 收集所有 ts_code
        all_codes = list({t["ticker"] for t in trades_input})

        # 确定时间范围
        timestamps = []
        for t in trades_input:
            if t["alert_time"]:
                try:
                    ts = self._parse_timestamp(t["alert_time"])
                    timestamps.append(ts)
                except Exception as e:
                    logger.debug(f"_run_pnl: 解析时间戳失败: {e}")

        if not timestamps:
            logger.debug("_run_pnl: 无有效时间戳，返回空结果")
            task_manager.update_progress(task_id, 100, "无有效时间戳")
            return {
                "trades": [],
                "summary": self._empty_summary(),
                "config": {
                    "holding_days": holding_days,
                    "total_alerts": total_alerts,
                    "min_confidence": min_confidence,
                },
            }

        min_date = (min(timestamps) - timedelta(days=5)).date()
        max_date = (max(timestamps) + timedelta(days=holding_days + 15)).date()

        # 从 DB 查询股价
        price_data = self._load_prices(task_id, all_codes, min_date, max_date)
        task_manager.update_progress(task_id, 50, f"已加载 {len(price_data)} 只 A 股的股价，正在计算 P&L...")

        # 逐笔计算
        results = []
        for i, trade in enumerate(trades_input):
            if i % 50 == 0:
                pct = 50 + int(40 * i / len(trades_input))
                task_manager.update_progress(task_id, pct, f"计算交易 {i+1}/{len(trades_input)}...")

            ticker = trade["ticker"]
            if ticker not in price_data or price_data[ticker].empty:
                logger.debug(f"_run_pnl: ticker={ticker} 无股价数据，跳过")
                continue

            df = price_data[ticker]
            alert_time = trade["alert_time"]
            if not alert_time:
                logger.debug(f"_run_pnl: ticker={ticker} alert_time 为空，跳过")
                continue

            try:
                ts = self._parse_timestamp(alert_time)
            except Exception as e:
                logger.debug(f"_run_pnl: ticker={ticker} 解析 alert_time 失败: {e}，跳过")
                continue

            entry_date = self._find_entry_date(ts, df.index)
            if entry_date is None:
                logger.debug(f"_run_pnl: ticker={ticker} 未找到入场日期，跳过")
                continue

            exit_date = self._find_exit_date(entry_date, holding_days, df.index)

            # Mark-to-market
            is_mtm = False
            if exit_date is None:
                later_days = df.index[df.index > entry_date]
                if later_days.empty:
                    exit_date = entry_date
                else:
                    exit_date = later_days[-1]
                is_mtm = True

            entry_price = float(df.loc[entry_date, "close"])
            exit_price = float(df.loc[exit_date, "close"])

            if entry_price <= 0 or not math.isfinite(entry_price) or not math.isfinite(exit_price):
                logger.debug(f"_run_pnl: ticker={ticker} 价格无效 entry={entry_price} exit={exit_price}，跳过")
                continue

            if trade["direction"] == "bearish":
                ret = (entry_price - exit_price) / entry_price
            else:
                ret = (exit_price - entry_price) / entry_price

            ret_pct = round(ret * 100, 2)

            # Benchmark: 同期买入持有（纯多）
            bm_ret_pct = round((exit_price - entry_price) / entry_price * 100, 2)

            actual_days = len(df.index[(df.index > entry_date) & (df.index <= exit_date)])

            results.append({
                "alert_idx": trade["alert_idx"],
                "ticker": ticker,
                "name": trade["name"],
                "direction": trade["direction"],
                "confidence": trade["confidence"],
                "alert_time": trade["alert_time"],
                "entry_date": str(entry_date),
                "entry_price": round(entry_price, 2),
                "exit_date": str(exit_date),
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
        summary = self._compute_summary(results)

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
        codes: list[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, pd.DataFrame]:
        """从 daily_price 表批量加载 A 股收盘价。"""
        result = {}
        session = self._db.get_session()
        try:
            # 批量查询
            batch_size = 50
            for i in range(0, len(codes), batch_size):
                batch = codes[i:i + batch_size]
                pct = 15 + int(35 * i / len(codes))
                task_manager.update_progress(task_id, pct, f"加载 A 股股价 ({i+1}-{min(i+batch_size, len(codes))}/{len(codes)})...")

                placeholders = ",".join([f":c{j}" for j in range(len(batch))])
                params = {f"c{j}": c for j, c in enumerate(batch)}
                params["start"] = start_date
                params["end"] = end_date

                sql = text(
                    f"SELECT ts_code, trade_date, `close`, is_limit_up, is_limit_down "
                    f"FROM daily_price "
                    f"WHERE ts_code IN ({placeholders}) "
                    f"AND trade_date BETWEEN :start AND :end "
                    f"ORDER BY trade_date"
                )

                rows = session.execute(sql, params).fetchall()

                # 分组为 DataFrame
                data_map: dict[str, list] = {}
                for row in rows:
                    data_map.setdefault(row[0], []).append({
                        "trade_date": row[1],
                        "close": row[2],
                        "is_limit_up": row[3],
                        "is_limit_down": row[4],
                    })

                for code, records in data_map.items():
                    df = pd.DataFrame(records)
                    df["trade_date"] = pd.to_datetime(df["trade_date"])
                    df = df.set_index("trade_date").sort_index()
                    result[code] = df

        except Exception as e:
            logger.error(f"加载 A 股股价失败: {e}")
        finally:
            session.close()

        return result

    @staticmethod
    def _parse_timestamp(ts_str: str) -> datetime:
        """解析时间字符串为 datetime。"""
        if isinstance(ts_str, datetime):
            return ts_str

        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ]:
            try:
                return datetime.strptime(ts_str, fmt)
            except ValueError:
                logger.debug(f"_parse_timestamp: 格式 {fmt} 不匹配，尝试下一个")
                continue

        if "+" in ts_str or ts_str.endswith("Z"):
            ts_str = ts_str.replace("Z", "+00:00")
            return datetime.fromisoformat(ts_str)

        raise ValueError(f"无法解析时间: {ts_str}")

    @staticmethod
    def _find_entry_date(alert_time: datetime, trading_days, max_search: int = 15):
        """
        A 股入场日期:
        - Polymarket 告警通常在美东时间 → 对应北京时间次日
        - 保守策略: 告警日期 +1 天起找下一个 A 股交易日 (T+1)
        """
        if trading_days.empty:
            logger.debug("_find_entry_date: 交易日序列为空，返回 None")
            return None

        alert_date = alert_time.date() if isinstance(alert_time, datetime) else alert_time
        # 告警通常在美股时段（北京时间凌晨），下一个 A 股交易日入场
        search_date = pd.Timestamp(alert_date) + pd.Timedelta(days=1)
        for _ in range(max_search):
            if search_date in trading_days:
                return search_date
            search_date += pd.Timedelta(days=1)
        logger.debug(f"_find_entry_date: 搜索 {max_search} 天后仍未找到交易日，返回 None")
        return None

    @staticmethod
    def _find_exit_date(entry_date, holding_days: int, trading_days, max_search: int = 30):
        """找到持有 N 个交易日后的出场日期。"""
        count = 0
        current = entry_date + pd.Timedelta(days=1)
        for _ in range(max_search + holding_days):
            if current in trading_days:
                count += 1
                if count >= holding_days:
                    return current
            current += pd.Timedelta(days=1)
        logger.debug(f"_find_exit_date: 持有 {holding_days} 交易日后未找到出场日期，返回 None")
        return None

    def _compute_summary(self, trades: list[dict]) -> dict:
        """计算汇总统计。"""
        total_trades = len(trades)
        if total_trades == 0:
            return self._empty_summary()

        mtm_count = sum(1 for t in trades if t.get("is_mark_to_market"))

        returns = [t["return_pct"] for t in trades]
        wins = [t for t in trades if t["is_win"]]
        losses = [t for t in trades if not t["is_win"]]

        win_count = len(wins)
        loss_count = len(losses)
        win_rate = win_count / total_trades

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

        # Sharpe ratio
        sharpe = 0.0
        if len(returns) >= 2:
            import statistics
            clean_returns = [r for r in returns if math.isfinite(r)]
            if len(clean_returns) >= 2:
                std = statistics.stdev(clean_returns)
                if std > 0:
                    clean_avg = sum(clean_returns) / len(clean_returns)
                    sharpe = round((clean_avg / std) * math.sqrt(242), 2)  # A 股约 242 个交易日

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
        top_losers = sorted_by_return[-10:][::-1]

        # 按 ticker 汇总
        ticker_stats_map: dict[str, list] = {}
        for t in trades:
            ticker_stats_map.setdefault(t["ticker"], []).append(t)
        ticker_stats = []
        for ticker, ts in ticker_stats_map.items():
            t_wins = [t for t in ts if t["is_win"]]
            ticker_stats.append({
                "ticker": ticker,
                "name": ts[0].get("name", ticker),
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
            "mtm_trades": mtm_count,
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
    def _empty_summary() -> dict:
        return {
            "total_trades": 0,
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
