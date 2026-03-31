"""Report generation API views."""
import logging

from rest_framework.decorators import api_view
from rest_framework.response import Response

from services.data.database import DatabaseManager
from tasks.manager import task_manager

logger = logging.getLogger(__name__)


def _get_db():
    db = DatabaseManager()
    db.init_tables()
    return db


@api_view(['POST'])
def generate_report(request):
    """Start report generation (async) - returns data for frontend rendering."""
    start_date = request.data.get('start_date', '2020-01-01')
    end_date = request.data.get('end_date', '2024-12-31')

    task_id = task_manager.submit(
        f'生成报告 {start_date}~{end_date}',
        _run_report,
        start_date, end_date,
    )
    return Response({'task_id': task_id})


def _run_report(task_id, start_date, end_date):
    """Generate report data."""
    from services.strategy.multi_factor import MultiFactorStrategy
    from services.strategy.backtest import BacktestEngine
    from services.risk.risk_manager import RiskManager
    from services.monitor.performance import PerformanceAnalyzer
    import pandas as pd

    db = _get_db()

    task_manager.update_progress(task_id, 10, '生成选股信号...')
    strategy = MultiFactorStrategy(db)
    signals = strategy.generate_signals(start_date, end_date)

    if not signals:
        raise ValueError('无有效信号')

    task_manager.update_progress(task_id, 30, '风控调整...')
    rm = RiskManager(db)
    adjusted = {}
    for dt, df_sig in signals.items():
        adjusted[dt] = rm.adjust_weights(df_sig, dt)

    task_manager.update_progress(task_id, 50, '执行回测...')
    engine = BacktestEngine(db)
    result = engine.run(adjusted, start_date, end_date)

    if not result:
        raise ValueError('回测失败')

    task_manager.update_progress(task_id, 70, '计算绩效...')
    nav = result['nav']
    benchmark = result.get('benchmark_nav')
    summary = engine.summary(result)

    # Industry attribution
    task_manager.update_progress(task_id, 85, '行业归因分析...')
    latest_signal_date = sorted(adjusted.keys())[-1]
    holdings = adjusted[latest_signal_date]

    analyzer = PerformanceAnalyzer(db)
    attribution = analyzer.industry_attribution(holdings, start_date, end_date)

    # Build response
    nav_data = []
    if nav is not None and not nav.empty:
        for dt, v in nav.items():
            nav_data.append({
                'date': dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt),
                'nav': round(float(v), 4),
            })

    benchmark_data = []
    if benchmark is not None and not benchmark.empty:
        for dt, v in benchmark.items():
            benchmark_data.append({
                'date': dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt),
                'nav': round(float(v), 4),
            })

    attr_data = []
    if attribution is not None and not attribution.empty:
        for _, row in attribution.iterrows():
            record = {}
            for col in attribution.columns:
                val = row[col]
                if pd.isna(val):
                    record[col] = None
                elif isinstance(val, float):
                    record[col] = round(val, 4)
                else:
                    record[col] = val
            attr_data.append(record)

    summary_dict = {}
    if summary is not None and not summary.empty:
        for _, row in summary.iterrows():
            summary_dict[row.iloc[0]] = row.iloc[1] if len(row) > 1 else None

    # Holdings
    holdings_data = []
    for _, row in holdings.iterrows():
        holdings_data.append({
            'ts_code': row.get('ts_code', ''),
            'weight': round(float(row.get('weight', 0)), 4),
            'score': round(float(row.get('score', 0)), 3) if pd.notna(row.get('score')) else None,
        })

    return {
        'summary': summary_dict,
        'nav': nav_data,
        'benchmark': benchmark_data,
        'attribution': attr_data,
        'holdings': holdings_data,
        'period': {'start': start_date, 'end': end_date},
    }
