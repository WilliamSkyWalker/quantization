"""A 股 Paper trading API views — Django ORM 版。

注意：
    底层 PaperTrader / 策略 / 风控仍在 services.execution / services.strategy /
    services.risk 中，P1 Phase 会迁到 trading/ + backtest/ apps。当前只把 views 层
    的 raw SQL 替换为 Django ORM。
"""
import logging

import pandas as pd
from django.db.models import Max
from rest_framework.decorators import api_view
from rest_framework.response import Response

from services.config import PAPER_INITIAL_CAPITAL
from stocks.models import ADailyPrice, AIndexDaily, APaperNav, APaperPosition, AStockBasic
from tasks.manager import task_manager

logger = logging.getLogger(__name__)


def _get_latest_trade_date():
    """取 daily_price 最新 trade_date。"""
    return ADailyPrice.objects.aggregate(m=Max("trade_date"))["m"]


def _get_latest_close_map(codes: list[str], trade_date) -> dict[str, float]:
    if not codes or trade_date is None:
        return {}
    qs = ADailyPrice.objects.filter(
        ts_code__in=codes, trade_date=trade_date,
    ).values_list("ts_code", "close")
    return {c: float(p) for c, p in qs if p is not None}


def _get_name_map(codes: list[str]) -> dict[str, str]:
    if not codes:
        return {}
    qs = AStockBasic.objects.filter(ts_code__in=codes).values_list("ts_code", "name")
    return {c: (n or "") for c, n in qs}


@api_view(['GET'])
def paper_account(request):
    """获取 Paper Trading 账户总览（基于 APaperPosition + PAPER_INITIAL_CAPITAL）。"""
    positions = list(APaperPosition.objects.all())
    latest_td = _get_latest_trade_date()

    codes = [p.ts_code for p in positions]
    px_map = _get_latest_close_map(codes, latest_td)

    market_value = 0.0
    for p in positions:
        px = px_map.get(p.ts_code) or (p.avg_cost or 0.0)
        market_value += float(p.volume or 0) * float(px)

    # Cash 约等于 initial_capital - 持仓成本（简化口径）
    cost_value = sum(float(p.volume or 0) * float(p.avg_cost or 0) for p in positions)
    cash = PAPER_INITIAL_CAPITAL - cost_value
    total_assets = cash + market_value
    initial = float(PAPER_INITIAL_CAPITAL)

    return Response({
        'account_name': 'default',
        'initial_capital': initial,
        'cash': round(cash, 2),
        'total_assets': round(total_assets, 2),
        'market_value': round(market_value, 2),
        'pnl': round(total_assets - initial, 2),
        'pnl_pct': round(total_assets / initial - 1, 4) if initial > 0 else 0,
    })


@api_view(['GET'])
def paper_positions(request):
    """当前持仓。"""
    positions = list(APaperPosition.objects.all())
    if not positions:
        return Response({'price_date': None, 'positions': []})

    codes = [p.ts_code for p in positions]
    latest_td = _get_latest_trade_date()
    name_map = _get_name_map(codes)
    px_map = _get_latest_close_map(codes, latest_td)

    result = []
    for p in positions:
        current_price = float(px_map.get(p.ts_code) or p.avg_cost or 0)
        market_value = float(p.volume or 0) * current_price
        cost_value = float(p.volume or 0) * float(p.avg_cost or 0)
        pnl = market_value - cost_value
        result.append({
            'ts_code': p.ts_code,
            'name': name_map.get(p.ts_code, ''),
            'volume': int(p.volume or 0),
            'cost_basis': round(float(p.avg_cost or 0), 2),
            'current_price': round(current_price, 2),
            'market_value': round(market_value, 2),
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl / cost_value, 4) if cost_value > 0 else 0,
        })

    return Response({
        'price_date': latest_td.strftime("%Y-%m-%d") if latest_td else None,
        'positions': result,
    })


@api_view(['GET'])
def paper_nav(request):
    """NAV 历史 + 沪深 300 基准。"""
    days = int(request.query_params.get('days', 30))

    # 取最近 N 条 NAV
    nav_rows = list(
        APaperNav.objects.order_by("-trade_date").values("trade_date", "nav")[:days]
    )
    nav_list = [
        {
            "date": r["trade_date"].strftime("%Y-%m-%d"),
            "nav": round(float(r["nav"]), 4),
        }
        for r in nav_rows
    ]
    nav_list.sort(key=lambda x: x["date"])

    # 基准
    benchmark = []
    if nav_list:
        first_date = pd.to_datetime(nav_list[0]["date"]).date()
        last_date = pd.to_datetime(nav_list[-1]["date"]).date()
        idx_rows = list(
            AIndexDaily.objects.filter(
                ts_code="000300.SH",
                trade_date__gte=first_date, trade_date__lte=last_date,
            ).order_by("trade_date").values("trade_date", "close")
        )
        if idx_rows:
            base = float(idx_rows[0]["close"])
            for r in idx_rows:
                benchmark.append({
                    "date": r["trade_date"].strftime("%Y-%m-%d"),
                    "nav": round(float(r["close"]) / base, 4),
                })

    return Response({'nav': nav_list, 'benchmark': benchmark})


@api_view(['GET'])
def paper_transactions(request):
    """交易历史。"""
    from stocks.models import APaperTransaction

    last_n = int(request.query_params.get('last', 50))
    trade_date = request.query_params.get('date')

    q = APaperTransaction.objects.all().order_by("-trade_date", "-id")
    if trade_date:
        q = q.filter(trade_date=pd.to_datetime(trade_date).date())
    q = q[:last_n]

    rows = list(q.values(
        "trade_date", "ts_code", "name", "direction", "volume",
        "price", "amount", "fee", "reason",
    ))
    if not rows:
        return Response([])

    codes = list({r["ts_code"] for r in rows})
    name_map = _get_name_map(codes)

    result = []
    for r in rows:
        record = {
            "trade_date": r["trade_date"].strftime("%Y-%m-%d") if r.get("trade_date") else None,
            "ts_code": r["ts_code"],
            "name": r.get("name") or name_map.get(r["ts_code"], ""),
            "direction": r["direction"],
            "volume": r["volume"],
            "price": round(float(r["price"]), 4) if r["price"] is not None else None,
            "amount": round(float(r["amount"]), 4) if r["amount"] is not None else None,
            "fee": round(float(r["fee"]), 4) if r["fee"] is not None else None,
            "reason": r.get("reason", ""),
        }
        result.append(record)

    return Response(result)


@api_view(['POST'])
def paper_trade(request):
    """执行当日交易信号（异步）。"""
    task_id = task_manager.submit('执行交易信号', _run_trade)
    return Response({'task_id': task_id})


@api_view(['POST'])
def paper_replay(request):
    """历史回放（异步）。"""
    start_date = request.data.get('start_date', '2020-01-01')
    end_date = request.data.get('end_date', '2024-12-31')
    reset = request.data.get('reset', False)
    capital = request.data.get('capital', PAPER_INITIAL_CAPITAL)

    task_id = task_manager.submit(
        f'回放 {start_date}~{end_date}',
        _run_replay, start_date, end_date, reset, capital,
    )
    return Response({'task_id': task_id})


@api_view(['POST'])
def paper_reset(request):
    """重置 Paper Trading 账户。"""
    # DatabaseManager 已废弃
    from trading.services.a_paper_trader import PaperTrader

    db = None  # DatabaseManager 已废弃
    trader = PaperTrader(db)
    trader.connect()
    trader.reset_account()
    return Response({'message': '模拟账户已重置'})


def _run_trade(task_id):
    """T+1 交易信号（底层仍调 services.strategy / services.execution，P1 迁）。"""
    # DatabaseManager 已废弃
    from trading.services.a_paper_trader import PaperTrader
    from trading.services.a_risk import RiskManager
    from backtest.services.a_strategy import MultiFactorStrategy

    db = None  # DatabaseManager 已废弃
    trader = PaperTrader(db)
    trader.connect()

    task_manager.update_progress(task_id, 10, '获取交易日期...')
    recent_dates = list(
        ADailyPrice.objects.order_by("-trade_date")
        .values_list("trade_date", flat=True).distinct()[:2]
    )
    if not recent_dates:
        raise ValueError('无行情数据')

    dates_sorted = sorted(d.strftime("%Y-%m-%d") for d in recent_dates)
    signal_date = dates_sorted[0] if len(dates_sorted) >= 2 else dates_sorted[-1]
    exec_date = dates_sorted[-1]

    task_manager.update_progress(task_id, 30, f'生成 {signal_date} 选股信号...')
    strategy = MultiFactorStrategy(db)
    signal = strategy.select_stocks(signal_date)

    if signal.empty:
        raise ValueError(f'选股日 {signal_date} 无有效信号')

    task_manager.update_progress(task_id, 60, '风控调整...')
    rm = RiskManager(db)
    adjusted = rm.adjust_weights(signal, signal_date)

    task_manager.update_progress(task_id, 80, f'在 {exec_date} 执行交易...')
    result = trader.sync_position(adjusted, trade_date=exec_date)

    return {
        'signal_date': signal_date,
        'exec_date': exec_date,
        'stocks': len(adjusted),
        'result': str(result),
    }


def _run_replay(task_id, start_date, end_date, reset, capital):
    """历史回放。底层 trader / strategy / risk 仍在 services/，待 P1 迁。"""
    # DatabaseManager 已废弃
    from trading.services.a_paper_trader import PaperTrader
    from trading.services.a_risk import RiskManager
    from backtest.services.a_strategy import MultiFactorStrategy

    db = None  # DatabaseManager 已废弃
    trader = PaperTrader(db)
    trader.connect(initial_capital=capital)

    if reset:
        task_manager.update_progress(task_id, 5, '重置模拟账户...')
        trader.reset_account()

    task_manager.update_progress(task_id, 10, '生成选股信号...')
    strategy = MultiFactorStrategy(db)
    signals = strategy.generate_signals(start_date, end_date)

    if not signals:
        raise ValueError('无有效信号')

    task_manager.update_progress(task_id, 40, '风控调整...')
    rm = RiskManager(db)
    adjusted = {dt: rm.adjust_weights(df_sig, dt) for dt, df_sig in signals.items()}

    task_manager.update_progress(task_id, 60, '回放交易...')
    trader.replay(adjusted, start_date, end_date)

    task_manager.update_progress(task_id, 90, '计算绩效...')
    nav_series = trader.get_nav_series()

    nav_data = []
    if not nav_series.empty:
        for dt, v in nav_series.items():
            nav_data.append({
                'date': dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt),
                'nav': round(float(v), 4),
            })

    return {
        'start_date': start_date,
        'end_date': end_date,
        'signal_count': len(signals),
        'nav': nav_data,
    }
