"""US stock multi-factor strategy API views."""
import logging

from rest_framework.decorators import api_view
from rest_framework.response import Response

from services.data.database import DatabaseManager
from services.data.us_cleaner import get_us_clean_universe
from services.strategy.us_multi_factor import USMultiFactorStrategy
from services.strategy.us_backtest import USBacktestEngine
from services.execution.us_paper_trader import USPaperTrader
from tasks.manager import task_manager

logger = logging.getLogger(__name__)

_db = DatabaseManager()


# ============================================================
# 美股股票池
# ============================================================

@api_view(['GET'])
def universe(request):
    """获取美股可交易股票池。"""
    date = request.query_params.get('date')
    if not date:
        import datetime
        date = datetime.date.today().strftime('%Y-%m-%d')

    df = get_us_clean_universe(_db, date)
    return Response({
        'date': date,
        'count': len(df),
        'data': df.to_dict(orient='records'),
    })


# ============================================================
# 美股选股
# ============================================================

@api_view(['POST'])
def select_stocks(request):
    """运行美股多因子选股（后台任务）。"""
    date = request.data.get('date')
    if not date:
        import datetime
        date = datetime.date.today().strftime('%Y-%m-%d')

    def _run(task_id):
        task_manager.update_progress(task_id, 10, '初始化策略...')
        strategy = USMultiFactorStrategy(_db)
        task_manager.update_progress(task_id, 30, '计算因子...')
        result = strategy.select_stocks(date)
        if result is None or result.empty:
            return {'date': date, 'count': 0, 'data': []}
        task_manager.update_progress(task_id, 90, '生成结果...')
        return {
            'date': date,
            'count': len(result),
            'data': result.to_dict(orient='records'),
        }

    tid = task_manager.submit('美股选股', _run)
    return Response({'status': 'started', 'task_id': tid})


# ============================================================
# 美股回测
# ============================================================

@api_view(['POST'])
def backtest_run(request):
    """运行美股回测（后台任务）。"""
    start_date = request.data.get('start_date')
    end_date = request.data.get('end_date')
    initial_capital = float(request.data.get('initial_capital', 100000))
    strategy_type = request.data.get('strategy', 'alpha')  # alpha | beta | baseline

    if not start_date or not end_date:
        return Response({'error': 'start_date 和 end_date 为必填项'}, status=400)

    def _run(task_id):
        task_manager.update_progress(task_id, 5, '初始化策略...')
        if strategy_type == 'beta':
            from services.strategy.us_beta_strategy import USBetaStrategy
            strategy = USBetaStrategy(_db)
        elif strategy_type == 'baseline':
            from services.strategy.us_baseline_strategy import USBaselineStrategy
            strategy = USBaselineStrategy(_db)
        else:
            strategy = USMultiFactorStrategy(_db)

        task_manager.update_progress(task_id, 10, '生成信号...')
        signals = strategy.generate_signals(start_date, end_date)

        task_manager.update_progress(task_id, 70, '运行回测...')
        engine = USBacktestEngine(
            initial_capital=initial_capital,
            risk_controls=(strategy_type != 'baseline'),
        )
        result = engine.run(signals, start_date, end_date, task_id=task_id)

        task_manager.update_progress(task_id, 95, '计算统计...')

        # 序列化
        nav = result.get('nav')
        benchmark = result.get('benchmark_nav')
        trades = result.get('trades')
        stats = result.get('stats', {})

        nav_data = []
        if nav is not None and not nav.empty:
            for dt, val in nav.items():
                nav_data.append({'date': str(dt)[:10], 'nav': round(float(val), 6)})

        bench_data = []
        if benchmark is not None and not benchmark.empty:
            for dt, val in benchmark.items():
                bench_data.append({'date': str(dt)[:10], 'nav': round(float(val), 6)})

        trade_data = []
        if trades is not None and not trades.empty:
            trade_data = trades.head(2000).to_dict(orient='records')

        return {
            'nav': nav_data,
            'benchmark': bench_data,
            'trades': trade_data,
            'stats': stats,
        }

    tid = task_manager.submit('美股回测', _run)
    return Response({'status': 'started', 'task_id': tid})


# ============================================================
# 美股模拟交易
# ============================================================

@api_view(['GET'])
def paper_account(request):
    """获取美股模拟账户信息。"""
    trader = USPaperTrader(_db)
    trader.connect()
    return Response(trader.get_account_info())


@api_view(['GET'])
def paper_positions(request):
    """获取美股模拟持仓。"""
    trader = USPaperTrader(_db)
    trader.connect()
    df = trader.get_current_positions()
    return Response({'data': df.to_dict(orient='records') if not df.empty else []})


@api_view(['GET'])
def paper_nav(request):
    """获取美股模拟交易 NAV 历史。"""
    trader = USPaperTrader(_db)
    trader.connect()
    df = trader.get_nav_history()
    return Response({'data': df.to_dict(orient='records') if not df.empty else []})


@api_view(['POST'])
def paper_trade(request):
    """执行美股模拟交易（运行选股 + 调仓）。"""
    date = request.data.get('date')

    def _run(task_id):
        task_manager.update_progress(task_id, 10, '运行选股...')
        strategy = USMultiFactorStrategy(_db)

        if not date:
            import datetime
            trade_date = datetime.date.today().strftime('%Y-%m-%d')
        else:
            trade_date = date

        result = strategy.select_stocks(trade_date)
        if result is None or result.empty:
            return {'message': '无选股结果', 'trades': 0}

        task_manager.update_progress(task_id, 60, '执行调仓...')
        trader = USPaperTrader(_db)
        trader.connect()
        trades = trader.sync_position(result[['ticker', 'weight']])
        trader.update_nav()

        return {
            'message': f'调仓完成，执行 {trades} 笔交易',
            'trades': trades,
            'account': trader.get_account_info(),
        }

    tid = task_manager.submit('美股模拟交易', _run)
    return Response({'status': 'started', 'task_id': tid})


@api_view(['POST'])
def paper_reset(request):
    """重置美股模拟账户。"""
    trader = USPaperTrader(_db)
    trader.connect()
    trader.reset()
    return Response({'message': '美股模拟账户已重置'})
