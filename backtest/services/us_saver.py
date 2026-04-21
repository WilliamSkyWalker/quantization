"""回测结果存库 — CLI / API 统一调用"""

import json
import logging

import numpy as np
import pandas as pd

from backtest.models import BacktestResult

logger = logging.getLogger(__name__)


def _calc_monthly_returns(nav_series: pd.Series) -> list[dict]:
    """从 NAV Series 计算月度收益率。"""
    monthly = []
    if nav_series is None or nav_series.empty:
        logger.debug("_calc_monthly_returns: NAV 为空，返回空列表")
        return monthly

    nav_series = nav_series.sort_index()
    prev_nav = nav_series.iloc[0]
    current_month = None
    month_start_nav = None

    for dt, val in nav_series.items():
        ym = (
            (dt.year, dt.month)
            if hasattr(dt, "year")
            else (int(str(dt)[:4]), int(str(dt)[5:7]))
        )
        if current_month is None:
            current_month = ym
            month_start_nav = val
            continue
        if ym != current_month:
            ret = (val / month_start_nav - 1) if month_start_nav else 0
            monthly.append(
                {"year": current_month[0], "month": current_month[1], "return": round(float(ret), 4)}
            )
            current_month = ym
            month_start_nav = val

    # 最后一个月
    if current_month and month_start_nav and nav_series.iloc[-1] != month_start_nav:
        ret = (nav_series.iloc[-1] / month_start_nav - 1) if month_start_nav else 0
        monthly.append(
            {"year": current_month[0], "month": current_month[1], "return": round(float(ret), 4)}
        )

    return monthly


def _serialize_nav(nav: pd.Series) -> list[dict]:
    """NAV Series → [{date, nav}]"""
    if nav is None or nav.empty:
        logger.debug("_serialize_nav: NAV 为空")
        return []
    data = []
    for dt, v in nav.items():
        data.append({
            "date": dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10],
            "nav": round(float(v), 6),
        })
    return data


def _serialize_trades(trades, market: str) -> list[dict]:
    """交易记录 DataFrame → JSON list (supports polars and pandas)"""
    if trades is None:
        logger.debug("_serialize_trades: 交易记录为空")
        return []

    # polars DataFrame
    try:
        import polars as pl
        if isinstance(trades, pl.DataFrame):
            if trades.is_empty():
                return []
            ticker_col = "ticker" if market == "us" else "ts_code"
            data = []
            for row in trades.iter_rows(named=True):
                entry = {
                    "date": str(row.get("date", "")),
                    "ticker": row.get(ticker_col, ""),
                    "direction": row.get("direction", ""),
                    "price": round(float(row.get("price", 0) or 0), 2),
                }
                vol = row.get("volume", 0)
                entry["volume"] = int(vol) if vol is not None else 0
                amt = row.get("amount", 0)
                entry["amount"] = round(float(amt or 0), 2) if amt is not None else 0
                data.append(entry)
            return data
    except ImportError:
        pass

    # pandas DataFrame fallback
    if isinstance(trades, pd.DataFrame) and trades.empty:
        logger.debug("_serialize_trades: 交易记录为空")
        return []

    ticker_col = "ticker" if market == "us" else "ts_code"
    data = []
    for _, row in trades.iterrows():
        entry = {
            "date": str(row.get("date", "")),
            "ticker": row.get(ticker_col, ""),
            "direction": row.get("direction", ""),
            "price": round(float(row.get("price", 0)), 2),
        }
        vol = row.get("volume", 0)
        entry["volume"] = int(vol) if pd.notna(vol) else 0
        amt = row.get("amount", 0)
        entry["amount"] = round(float(amt), 2) if pd.notna(amt) else 0
        data.append(entry)
    return data


def _calc_drawdown(nav: pd.Series) -> list[dict]:
    """NAV Series → 回撤序列"""
    if nav is None or nav.empty:
        logger.debug("_calc_drawdown: NAV 为空")
        return []
    cummax = nav.cummax()
    dd = (nav - cummax) / cummax
    data = []
    for dt, v in dd.items():
        data.append({
            "date": dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10],
            "drawdown": round(float(v), 4),
        })
    return data


class _NumpyEncoder(json.JSONEncoder):
    """处理 numpy 类型的 JSON encoder"""
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return round(float(obj), 6)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        return super().default(obj)


def save_backtest_result(
    market: str,
    strategy_type: str,
    start_date: str,
    end_date: str,
    result: dict,
    **kwargs,
) -> bool:
    """
    统一存储回测结果到 backtest_result 表（Django ORM）。

    Returns:
        True 存储成功，False 失败
    """
    nav = result.get("nav")
    benchmark = result.get("benchmark_nav")
    trades = result.get("trades")
    stats = result.get("stats", {})

    nav_data = _serialize_nav(nav)
    bench_data = _serialize_nav(benchmark)
    trade_data = _serialize_trades(trades, market)
    monthly_data = _calc_monthly_returns(nav)
    drawdown_data = _calc_drawdown(nav)

    summary_dict = {}
    if isinstance(stats, dict):
        summary_dict = stats
    elif isinstance(stats, pd.DataFrame) and not stats.empty:
        for _, row in stats.iterrows():
            summary_dict[row.iloc[0]] = row.iloc[1] if len(row) > 1 else None

    try:
        BacktestResult.objects.create(
            market=market,
            strategy_type=strategy_type,
            start_date=start_date,
            end_date=end_date,
            summary=json.dumps(summary_dict, ensure_ascii=False, cls=_NumpyEncoder),
            nav=json.dumps(nav_data, ensure_ascii=False, cls=_NumpyEncoder),
            benchmark=json.dumps(bench_data, ensure_ascii=False, cls=_NumpyEncoder),
            trades=json.dumps(trade_data, ensure_ascii=False, cls=_NumpyEncoder),
            monthly=json.dumps(monthly_data, ensure_ascii=False, cls=_NumpyEncoder),
            drawdown=json.dumps(drawdown_data, ensure_ascii=False, cls=_NumpyEncoder),
        )
        logger.info(f"回测结果已保存: market={market}, strategy={strategy_type}, {start_date}~{end_date}")
        return True
    except Exception as e:
        logger.warning(f"回测结果保存失败: {e}")
        return False
