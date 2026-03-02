"""Paper trading API views."""
import logging

import pandas as pd
from rest_framework.decorators import api_view
from rest_framework.response import Response

from backend.services.data.database import DatabaseManager
from backend.services.config import PAPER_INITIAL_CAPITAL
from backend.tasks.manager import task_manager

logger = logging.getLogger(__name__)


def _get_db():
    db = DatabaseManager()
    db.init_tables()
    return db


def _get_trader(db):
    from backend.services.execution.paper_trader import PaperTrader
    trader = PaperTrader(db)
    trader.connect()
    return trader


@api_view(['GET'])
def paper_account(request):
    """Get paper trading account overview."""
    db = _get_db()
    trader = _get_trader(db)
    report = trader.get_position_report()
    # Parse the account info from DB
    from backend.services.config import PAPER_ACCOUNT_NAME
    session = db.get_session()
    from backend.services.data.database import PaperAccount
    acct = session.query(PaperAccount).filter_by(account_name=PAPER_ACCOUNT_NAME).first()
    session.close()

    if not acct:
        return Response({
            'account_name': PAPER_ACCOUNT_NAME,
            'initial_capital': PAPER_INITIAL_CAPITAL,
            'cash': PAPER_INITIAL_CAPITAL,
            'total_assets': PAPER_INITIAL_CAPITAL,
            'positions': [],
        })

    return Response({
        'account_name': acct.account_name,
        'initial_capital': float(acct.initial_capital),
        'cash': float(acct.cash),
        'total_assets': float(acct.total_assets),
        'pnl': float(acct.total_assets - acct.initial_capital),
        'pnl_pct': round(float(acct.total_assets / acct.initial_capital - 1), 4),
    })


@api_view(['GET'])
def paper_positions(request):
    """Get current positions."""
    db = _get_db()
    from backend.services.config import PAPER_ACCOUNT_NAME
    from backend.services.data.database import PaperPosition
    session = db.get_session()
    positions = session.query(PaperPosition).filter_by(account_name=PAPER_ACCOUNT_NAME).all()

    # Get stock names
    codes = [p.ts_code for p in positions]
    name_map = {}
    if codes:
        codes_str = "','".join(codes)
        df_names = db.query(
            f"SELECT ts_code, name FROM stock_basic WHERE ts_code IN ('{codes_str}')"
        )
        name_map = dict(zip(df_names['ts_code'], df_names['name']))

    result = []
    for p in positions:
        market_value = float(p.market_value) if p.market_value else float(p.volume * p.current_price)
        cost_value = float(p.volume * p.cost_basis)
        pnl = market_value - cost_value
        result.append({
            'ts_code': p.ts_code,
            'name': name_map.get(p.ts_code, ''),
            'volume': int(p.volume),
            'cost_basis': round(float(p.cost_basis), 2),
            'current_price': round(float(p.current_price), 2),
            'market_value': round(market_value, 2),
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl / cost_value, 4) if cost_value > 0 else 0,
        })
    session.close()

    return Response(result)


@api_view(['GET'])
def paper_nav(request):
    """Get NAV history."""
    db = _get_db()
    trader = _get_trader(db)
    days = int(request.query_params.get('days', 30))
    df = trader.get_nav_history(last_n=days)

    if df.empty:
        return Response([])

    result = []
    for _, row in df.iterrows():
        result.append({
            'date': str(row.get('trade_date', row.get('date', ''))),
            'nav': round(float(row.get('nav', row.get('total_assets', 0))), 4),
        })
    return Response(result)


@api_view(['GET'])
def paper_transactions(request):
    """Get transaction history."""
    db = _get_db()
    trader = _get_trader(db)
    last_n = int(request.query_params.get('last', 50))
    trade_date = request.query_params.get('date')
    df = trader.get_transactions(trade_date=trade_date, last_n=last_n)

    if df.empty:
        return Response([])

    # Get stock names
    if 'ts_code' in df.columns:
        codes = df['ts_code'].unique().tolist()
        codes_str = "','".join(codes)
        df_names = db.query(
            f"SELECT ts_code, name FROM stock_basic WHERE ts_code IN ('{codes_str}')"
        )
        name_map = dict(zip(df_names['ts_code'], df_names['name']))
    else:
        name_map = {}

    result = []
    for _, row in df.iterrows():
        record = {}
        for col in df.columns:
            val = row[col]
            if pd.isna(val):
                record[col] = None
            elif isinstance(val, float):
                record[col] = round(val, 4)
            else:
                record[col] = val
        record['name'] = name_map.get(row.get('ts_code', ''), '')
        result.append(record)

    return Response(result)


@api_view(['POST'])
def paper_trade(request):
    """Execute today's trading signal (T+1 model)."""
    task_id = task_manager.submit('执行交易信号', _run_trade)
    return Response({'task_id': task_id})


@api_view(['POST'])
def paper_replay(request):
    """Start historical replay (async)."""
    start_date = request.data.get('start_date', '2020-01-01')
    end_date = request.data.get('end_date', '2024-12-31')
    reset = request.data.get('reset', False)
    capital = request.data.get('capital', PAPER_INITIAL_CAPITAL)

    task_id = task_manager.submit(
        f'回放 {start_date}~{end_date}',
        _run_replay,
        start_date, end_date, reset, capital,
    )
    return Response({'task_id': task_id})


@api_view(['POST'])
def paper_reset(request):
    """Reset paper trading account."""
    db = _get_db()
    trader = _get_trader(db)
    trader.reset_account()
    return Response({'message': '模拟账户已重置'})


def _run_trade(task_id):
    """Execute T+1 trade signal."""
    from backend.services.strategy.multi_factor import MultiFactorStrategy
    from backend.services.risk.risk_manager import RiskManager

    db = _get_db()
    trader = _get_trader(db)

    task_manager.update_progress(task_id, 10, '获取交易日期...')
    df_dates = db.query(
        "SELECT DISTINCT trade_date FROM daily_price ORDER BY trade_date DESC LIMIT 2"
    )
    if df_dates.empty:
        raise ValueError('无行情数据')

    dates = sorted(
        pd.to_datetime(df_dates['trade_date']).dt.strftime('%Y-%m-%d').tolist()
    )
    signal_date = dates[-2] if len(dates) >= 2 else dates[-1]
    exec_date = dates[-1]

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
    """Run paper trading replay."""
    from backend.services.strategy.multi_factor import MultiFactorStrategy
    from backend.services.risk.risk_manager import RiskManager
    from backend.services.execution.paper_trader import PaperTrader

    db = _get_db()
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
    adjusted = {}
    for dt, df_sig in signals.items():
        adjusted[dt] = rm.adjust_weights(df_sig, dt)

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
