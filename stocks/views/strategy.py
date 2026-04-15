"""US stock multi-factor strategy API views."""
import logging

from rest_framework.decorators import api_view
from rest_framework.response import Response

from stocks.services.cleaner import get_us_clean_universe
from backtest.services.strategy import USMultiFactorStrategy
from backtest.services.engine import USBacktestEngine
from services.execution.us_paper_trader import USPaperTrader
from services.execution.alpaca_trader import AlpacaTrader
from tasks.manager import task_manager

logger = logging.getLogger(__name__)


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

    df = get_us_clean_universe(date)
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
            logger.debug(f"select_stocks: 美股选股结果为空, date={date}")
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
            from backtest.services.beta import USBetaStrategy
            strategy = USBetaStrategy(_db)
        elif strategy_type == 'baseline':
            from backtest.services.baseline import USBaselineStrategy
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

        task_manager.update_progress(task_id, 95, '保存结果...')

        # 存库
        from backtest.services.saver import save_backtest_result
        save_backtest_result(_db, 'us', strategy_type, start_date, end_date, result)

        # 序列化返回给前端
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
            logger.debug(f"paper_trade: 美股模拟交易无选股结果, date={trade_date}")
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


# ============================================================
# Alpaca 模拟交易
# ============================================================

@api_view(['GET'])
def alpaca_account(request):
    """获取 Alpaca 模拟账户信息。"""
    trader = AlpacaTrader(_db)
    trader.connect()
    return Response(trader.get_account_info())


@api_view(['GET'])
def alpaca_positions(request):
    """获取 Alpaca 持仓。"""
    trader = AlpacaTrader(_db)
    trader.connect()
    df = trader.get_current_positions()
    return Response({'data': df.to_dict(orient='records') if not df.empty else []})


@api_view(['GET'])
def alpaca_orders(request):
    """获取 Alpaca 订单（open 或 closed）。"""
    status = request.query_params.get('status', 'open')
    trader = AlpacaTrader(_db)
    trader.connect()
    if status == 'closed':
        limit = int(request.query_params.get('limit', 50))
        df = trader.get_closed_orders(limit=limit)
    else:
        df = trader.get_open_orders()
    return Response({'data': df.to_dict(orient='records') if not df.empty else []})


@api_view(['POST'])
def alpaca_trade(request):
    """执行 Alpaca 模拟交易（选股 + 调仓）。"""
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
            logger.debug(f"alpaca_trade: 无选股结果, date={trade_date}")
            return {'message': '无选股结果', 'trades': 0}

        task_manager.update_progress(task_id, 60, '提交 Alpaca 订单...')
        trader = AlpacaTrader(_db)
        trader.connect()
        trades = trader.sync_position(result[['ticker', 'weight']])
        trader.update_nav()

        return {
            'message': f'Alpaca 调仓完成',
            'trades': trades,
            'account': trader.get_account_info(),
        }

    tid = task_manager.submit('Alpaca模拟交易', _run)
    return Response({'status': 'started', 'task_id': tid})


@api_view(['POST'])
def alpaca_reconcile(request):
    """对账：比较目标权重 vs Alpaca 实际持仓。"""
    date = request.data.get('date')
    if not date:
        import datetime
        date = datetime.date.today().strftime('%Y-%m-%d')

    strategy = USMultiFactorStrategy(_db)
    result = strategy.select_stocks(date)
    if result is None or result.empty:
        logger.debug(f"alpaca_reconcile: 无选股结果, date={date}")
        return Response({'message': '无选股结果', 'data': []})

    trader = AlpacaTrader(_db)
    trader.connect()
    diff = trader.reconcile(result[['ticker', 'weight']])
    return Response({'data': diff.to_dict(orient='records') if not diff.empty else []})


@api_view(['POST'])
def alpaca_reset(request):
    """关闭 Alpaca 所有持仓并取消挂单。"""
    trader = AlpacaTrader(_db)
    trader.connect()
    trader.reset()
    return Response({'message': 'Alpaca 持仓已清空，挂单已取消'})
