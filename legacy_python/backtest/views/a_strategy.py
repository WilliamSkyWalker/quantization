"""A 股选股 / 回测 API views — Django ORM 版。"""
import json
import logging

import pandas as pd
from django.db.models import Max
from rest_framework.decorators import api_view
from rest_framework.response import Response

from stocks.models import (
    ADailyPrice,
    AFactorSnapshot,
    AIndustryClass,
    ASelectionResult,
    AStockBasic,
)
from tasks.manager import task_manager

logger = logging.getLogger(__name__)


# ----------------------------------------------------------
# 工具
# ----------------------------------------------------------

def _get_latest_trade_date_str() -> str | None:
    latest = ADailyPrice.objects.aggregate(m=Max("trade_date"))["m"]
    return latest.strftime("%Y-%m-%d") if latest else None


def _name_map(codes: list[str]) -> dict[str, str]:
    if not codes:
        return {}
    return {
        c: (n or "")
        for c, n in AStockBasic.objects.filter(ts_code__in=codes).values_list("ts_code", "name")
    }


def _industry_map(codes: list[str]) -> dict[str, str]:
    if not codes:
        return {}
    return {
        r["ts_code"]: r["index_name"]
        for r in AIndustryClass.objects.filter(
            ts_code__in=codes, src="SW2021", level="L1", out_date__isnull=True,
        ).values("ts_code", "index_name")
    }


# ----------------------------------------------------------
# Universe (股票池)
# ----------------------------------------------------------

@api_view(['GET'])
def universe(request):
    """获取某日的可交易股票池。"""
    from stocks.services.a_cleaner import get_clean_universe
    # DatabaseManager 已废弃

    db = None  # DatabaseManager 已废弃
    date = request.query_params.get('date')
    if not date:
        date = _get_latest_trade_date_str()
    if not date:
        return Response({'error': '无行情数据'}, status=400)

    df = get_clean_universe(db, date)
    if df.empty:
        logger.debug(f"universe: 股票池为空, date={date}")
        return Response({'date': date, 'stocks': [], 'total': 0})

    industry_dist = {}
    if 'industry_name' in df.columns:
        industry_dist = df['industry_name'].value_counts().to_dict()

    limit_up = int(df['is_limit_up'].sum()) if 'is_limit_up' in df.columns else 0
    limit_down = int(df['is_limit_down'].sum()) if 'is_limit_down' in df.columns else 0

    cols = ['ts_code', 'name', 'industry_name', 'close', 'amount',
            'pct_chg', 'is_limit_up', 'is_limit_down']
    available_cols = [c for c in cols if c in df.columns]
    stocks = df[available_cols].head(500).to_dict('records')

    return Response({
        'date': date,
        'total': len(df),
        'limit_up': limit_up,
        'limit_down': limit_down,
        'industry_distribution': industry_dist,
        'stocks': stocks,
    })


# ----------------------------------------------------------
# Select stocks (选股)
# ----------------------------------------------------------

@api_view(['POST'])
def select_stocks(request):
    """启动异步选股任务。"""
    requested_date = request.data.get('date') or request.query_params.get('date')
    latest = _get_latest_trade_date_str()
    if not latest:
        return Response({'error': '无行情数据，请先下载行情'}, status=400)

    if not requested_date:
        date = latest
        fallback = False
    else:
        if requested_date > latest:
            date = latest
            fallback = True
        else:
            date = requested_date
            fallback = False

    task_id = task_manager.submit(f'选股 {date}', _run_select, date)
    return Response({
        'task_id': task_id, 'date': date,
        'fallback': fallback, 'requested_date': requested_date or date,
    })


def _run_select(task_id, date):
    """后台线程执行选股。"""
    # DatabaseManager 已废弃
    from backtest.services.a_strategy import MultiFactorStrategy

    db = None  # DatabaseManager 已废弃

    task_manager.update_progress(task_id, 10, '计算因子打分...')
    strategy = MultiFactorStrategy(db)
    scored = strategy.score_all_stocks(date, include_factors=True)

    if scored.empty:
        logger.debug(f"_run_select: 选股结果为空, date={date}")
        return {'date': date, 'stocks': [], 'total': 0, 'top_stocks': [], 'by_industry': {}}

    task_manager.update_progress(task_id, 80, '整理结果...')

    codes = scored['ts_code'].tolist()
    name_map = _name_map(codes)
    scored['name'] = scored['ts_code'].map(name_map).fillna('')

    if 'industry_name' not in scored.columns:
        ind_map = _industry_map(codes)
        scored['industry_name'] = scored['ts_code'].map(ind_map)

    scored['industry_name'] = scored['industry_name'].fillna('未知')

    scored = scored.sort_values('score', ascending=False)

    # 行情
    try:
        d = pd.to_datetime(date).date()
        price_rows = list(
            ADailyPrice.objects.filter(ts_code__in=codes, trade_date=d)
            .values("ts_code", "close", "pct_chg", "amount")
        )
        if price_rows:
            df_price = pd.DataFrame(price_rows)
            scored = scored.merge(df_price, on='ts_code', how='left')
    except Exception as e:
        logger.warning(f"_run_select: 合并行情失败: {e}")

    # Top N 权重
    top_n_df = scored.head(20).copy()
    if len(top_n_df) > 0:
        scores = top_n_df['score'].values
        shifted = pd.Series(scores).clip(lower=0).values
        total = shifted.sum()
        n = len(top_n_df)
        if total > 0:
            min_w = 1.0 / (n * 3)
            raw_w = shifted / total
            raw_w = pd.Series(raw_w).clip(lower=min_w).values
            top_n_df['weight'] = raw_w / raw_w.sum()
        else:
            top_n_df['weight'] = 1.0 / n

    top_n = top_n_df.to_dict('records')

    _DISPLAY_COLS = ['ts_code', 'score', 'industry_name', 'name', 'close', 'pct_chg', 'amount', 'weight']
    by_industry = {}
    for ind, grp in scored.groupby('industry_name'):
        grp5 = grp.head(5).copy()
        if len(grp5) > 0:
            scores_g = grp5['score'].values
            shifted_g = pd.Series(scores_g).clip(lower=0).values
            total_g = shifted_g.sum()
            n_g = len(grp5)
            if total_g > 0:
                min_w_g = 1.0 / (n_g * 3)
                raw_w_g = shifted_g / total_g
                raw_w_g = pd.Series(raw_w_g).clip(lower=min_w_g).values
                grp5['weight'] = raw_w_g / raw_w_g.sum()
            else:
                grp5['weight'] = 1.0 / n_g
        keep = [c for c in _DISPLAY_COLS if c in grp5.columns]
        by_industry[ind] = grp5[keep].to_dict('records')

    def clean_nan(obj):
        if isinstance(obj, float) and pd.isna(obj):
            return None
        if isinstance(obj, dict):
            return {k: clean_nan(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [clean_nan(v) for v in obj]
        return obj

    result = clean_nan({
        'date': date,
        'total': len(scored),
        'top_stocks': top_n,
        'by_industry': by_industry,
    })

    # 因子快照持久化
    _SKIP_COLS = {'ts_code', 'score', 'industry_name', 'name', 'close', 'pct_chg', 'amount', 'weight'}
    factor_cols = [c for c in scored.columns if c not in _SKIP_COLS]
    try:
        d = pd.to_datetime(date).date()
        AFactorSnapshot.objects.filter(date=d).delete()
        snapshots = []
        for _, row in scored.iterrows():
            fdict = {
                fc: (None if pd.isna(row[fc]) else float(row[fc]))
                for fc in factor_cols
                if fc in row.index
            }
            snapshots.append(AFactorSnapshot(
                date=d,
                ts_code=row['ts_code'],
                score=float(row['score']) if pd.notna(row.get('score')) else None,
                factors=json.dumps(fdict),
            ))
        AFactorSnapshot.objects.bulk_create(snapshots, batch_size=2000)
        logger.info(f"因子快照: {date} {len(snapshots)} 条")
    except Exception as e:
        logger.warning(f"因子快照保存失败: {e}")

    # 选股结果持久化
    try:
        d = pd.to_datetime(date).date()
        ASelectionResult.objects.update_or_create(
            date=d,
            defaults={
                "total": result['total'],
                "top_stocks": json.dumps(result['top_stocks'], ensure_ascii=False),
                "by_industry": json.dumps(result['by_industry'], ensure_ascii=False),
            },
        )
    except Exception as e:
        logger.warning(f"选股结果保存失败: {e}")

    return result


@api_view(['GET'])
def select_history(request):
    """已保存的选股日期列表。"""
    rows = list(
        ASelectionResult.objects.order_by("-date").values("date", "total", "updated_at")
    )
    return Response({
        'dates': [
            {
                'date': r['date'].strftime("%Y-%m-%d"),
                'total': int(r['total'] or 0),
                'updated_at': r['updated_at'].strftime("%Y-%m-%d %H:%M:%S") if r.get('updated_at') else None,
            }
            for r in rows
        ]
    })


@api_view(['GET'])
def select_history_date(request, date):
    """指定日期的选股结果。"""
    d = pd.to_datetime(date).date()
    row = ASelectionResult.objects.filter(date=d).values(
        "date", "total", "top_stocks", "by_industry", "updated_at"
    ).first()
    if row is None:
        return Response({'error': f'{date} 无保存的选股结果'}, status=404)
    return Response({
        'date': row['date'].strftime("%Y-%m-%d"),
        'total': int(row['total'] or 0),
        'top_stocks': json.loads(row['top_stocks']) if row['top_stocks'] else [],
        'by_industry': json.loads(row['by_industry']) if row['by_industry'] else {},
        'updated_at': row['updated_at'].strftime("%Y-%m-%d %H:%M:%S") if row.get('updated_at') else None,
    })


@api_view(['GET'])
def factor_detail(request):
    """某日某股的因子详情（快照优先，否则即时重算）。"""
    date = request.query_params.get('date')
    code = request.query_params.get('code')

    if not date or not code:
        return Response({'error': '需要 date 和 code 参数'}, status=400)

    try:
        d = pd.to_datetime(date).date()
        row = AFactorSnapshot.objects.filter(date=d, ts_code=code).values(
            "ts_code", "score", "factors"
        ).first()
        if row is not None:
            record = {'ts_code': row['ts_code'], 'score': row['score']}
            if row['factors']:
                record.update(json.loads(row['factors']))
            return Response(record)
    except Exception as e:
        logger.debug(f"factor_detail: 查快照失败，走重算: {e}")

    # DatabaseManager 已废弃
    from backtest.services.a_strategy import MultiFactorStrategy

    db = None  # DatabaseManager 已废弃
    strategy = MultiFactorStrategy(db)
    scored = strategy.score_all_stocks(date, include_factors=True)

    if scored.empty:
        return Response({'error': '无数据'}, status=404)

    row = scored[scored['ts_code'] == code]
    if row.empty:
        return Response({'error': f'{code} 不在股票池中'}, status=404)

    record = row.iloc[0].to_dict()
    record = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in record.items()}
    return Response(record)


# ----------------------------------------------------------
# Backtest
# ----------------------------------------------------------

@api_view(['POST'])
def backtest_run(request):
    """启动回测（异步）。"""
    start_date = request.data.get('start_date', '2020-01-01')
    end_date = request.data.get('end_date', '2024-12-31')

    task_id = task_manager.submit(
        f'回测 {start_date}~{end_date}',
        _run_backtest, start_date, end_date,
    )
    return Response({'task_id': task_id})


def _run_backtest(task_id, start_date, end_date):
    """后台执行回测（底层 multi_factor + BacktestEngine + RiskManager 仍在 services/，P1 迁）。"""
    # DatabaseManager 已废弃
    from trading.services.monitor.performance import PerformanceAnalyzer
    from trading.services.a_risk import RiskManager
    from backtest.services.a_engine import BacktestEngine
    from backtest.services.a_strategy import MultiFactorStrategy

    db = None  # DatabaseManager 已废弃

    def ensure_not_cancelled():
        if task_manager.is_cancelled(task_id):
            raise RuntimeError('回测已取消')

    task_manager.update_progress(task_id, 10, '生成选股信号...')
    strategy = MultiFactorStrategy(db)
    ensure_not_cancelled()
    cancel_check = lambda: task_manager.is_cancelled(task_id)  # noqa: E731
    signals = strategy.generate_signals(start_date, end_date, cancel_check=cancel_check)

    if not signals:
        raise ValueError('无有效信号')

    task_manager.update_progress(task_id, 40, '风控调整...')
    rm = RiskManager(db)
    adjusted = {}
    for dt, df_sig in signals.items():
        ensure_not_cancelled()
        adjusted[dt] = rm.adjust_weights(df_sig, dt)

    # 股票名映射（ORM）
    all_codes = set()
    for df_sig in adjusted.values():
        ensure_not_cancelled()
        all_codes.update(df_sig['ts_code'].tolist())
    name_map = _name_map(list(all_codes))

    task_manager.update_progress(task_id, 60, '执行回测...')
    engine = BacktestEngine(db)
    ensure_not_cancelled()
    result = engine.run(adjusted, start_date, end_date, cancel_check=cancel_check)

    if not result:
        raise ValueError('回测失败')

    task_manager.update_progress(task_id, 80, '计算绩效指标...')
    ensure_not_cancelled()
    summary = engine.summary(result)

    task_manager.update_progress(task_id, 88, '行业归因分析...')
    latest_signal_date = sorted(adjusted.keys())[-1]
    latest_holdings = adjusted[latest_signal_date]
    analyzer = PerformanceAnalyzer(db)
    ensure_not_cancelled()
    attribution = analyzer.industry_attribution(latest_holdings, start_date, end_date)

    attr_data = []
    if attribution is not None and not attribution.empty:
        for _, row in attribution.iterrows():
            ensure_not_cancelled()
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

    holdings_data = []
    for _, row in latest_holdings.iterrows():
        ensure_not_cancelled()
        holdings_data.append({
            'ts_code': row.get('ts_code', ''),
            'name': name_map.get(row.get('ts_code', ''), ''),
            'weight': round(float(row.get('weight', 0)), 4),
            'score': round(float(row.get('score', 0)), 3) if pd.notna(row.get('score')) else None,
        })

    nav = result.get('nav')
    benchmark = result.get('benchmark_nav')
    trades = result.get('trades')

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

    trade_data = []
    if trades is not None and not trades.empty:
        for _, row in trades.iterrows():
            trade_data.append({
                'date': str(row.get('date', '')),
                'ts_code': row.get('ts_code', ''),
                'name': name_map.get(row.get('ts_code', ''), ''),
                'direction': row.get('direction', ''),
                'price': round(float(row.get('price', 0)), 2),
                'volume': int(row.get('volume', 0)) if pd.notna(row.get('volume')) else 0,
                'amount': round(float(row.get('amount', 0)), 2) if pd.notna(row.get('amount')) else 0,
            })

    monthly_data = _calc_monthly_returns(nav) if nav is not None and not nav.empty else []

    drawdown_data = []
    if nav is not None and not nav.empty:
        cummax = nav.cummax()
        dd = (nav - cummax) / cummax
        for dt, v in dd.items():
            drawdown_data.append({
                'date': dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt),
                'drawdown': round(float(v), 4),
            })

    summary_dict = {}
    if summary is not None and not summary.empty:
        for _, row in summary.iterrows():
            summary_dict[row.iloc[0]] = row.iloc[1] if len(row) > 1 else None

    signal_data = []
    for dt in sorted(adjusted.keys()):
        raw = signals[dt]
        adj = adjusted[dt]
        merged = raw[['ts_code', 'score']].merge(
            adj[['ts_code', 'weight']], on='ts_code', how='right'
        ).sort_values('weight', ascending=False)
        stocks = []
        for _, r in merged.iterrows():
            stocks.append({
                'ts_code': r['ts_code'],
                'name': name_map.get(r['ts_code'], ''),
                'score': round(float(r['score']), 3) if pd.notna(r['score']) else None,
                'weight': round(float(r['weight']), 4),
            })
        signal_data.append({'date': dt, 'stocks': stocks})

    from backtest.services.us_saver import save_backtest_result
    save_backtest_result(db, 'cn', 'alpha', start_date, end_date, result)

    return {
        'summary': summary_dict,
        'nav': nav_data,
        'benchmark': benchmark_data,
        'trades': trade_data,
        'monthly': monthly_data,
        'drawdown': drawdown_data,
        'signals': signal_data,
        'attribution': attr_data,
        'holdings': holdings_data,
    }


@api_view(['GET'])
def backtest_history(request):
    """已保存的回测结果列表。"""
    import re
    from backtest.models.result import BacktestResult

    items = []
    for r in BacktestResult.objects.order_by("-created_at").values(
        "id", "start_date", "end_date", "summary", "created_at",
    ):
        headline = '-'
        head = (r.get('summary') or '')[:200]
        if head:
            try:
                m = re.search(r'"总收益":\s*"([^"]*)"', head)
                if m:
                    headline = m.group(1)
            except Exception as e:
                logger.debug(f"backtest_history: 解析摘要失败: {e}")
        items.append({
            'id': int(r['id']),
            'start_date': str(r['start_date'])[:10],
            'end_date': str(r['end_date'])[:10],
            'summary_headline': headline,
            'created_at': r['created_at'].strftime("%Y-%m-%d %H:%M:%S") if r.get('created_at') else None,
        })
    return Response({'items': items})


@api_view(['GET'])
def backtest_history_detail(request, pk):
    """按 id 取回测结果详情。"""
    from backtest.models.result import BacktestResult

    try:
        r = BacktestResult.objects.values().get(pk=int(pk))
    except BacktestResult.DoesNotExist:
        return Response({'error': '未找到该回测记录'}, status=404)

    def _load(field):
        val = r.get(field)
        if val:
            try:
                return json.loads(val)
            except Exception as e:
                logger.debug(f"backtest_history_detail._load: {field} JSON 失败: {e}")
        return {} if field == 'summary' else []

    return Response({
        'id': int(r['id']),
        'start_date': str(r['start_date'])[:10],
        'end_date': str(r['end_date'])[:10],
        'summary': _load('summary'),
        'nav': _load('nav'),
        'benchmark': _load('benchmark'),
        'trades': _load('trades'),
        'monthly': _load('monthly'),
        'drawdown': _load('drawdown'),
        'attribution': _load('attribution') if 'attribution' in r else [],
        'holdings': _load('holdings') if 'holdings' in r else [],
        'created_at': r['created_at'].strftime("%Y-%m-%d %H:%M:%S") if r.get('created_at') else None,
    })


# ----------------------------------------------------------
# 工具：月度收益率
# ----------------------------------------------------------

def _calc_monthly_returns(nav_series):
    monthly = []
    if nav_series is None or nav_series.empty:
        return monthly

    nav_series = nav_series.sort_index()
    current_month = None
    month_start_nav = None

    for dt, val in nav_series.items():
        ym = (dt.year, dt.month) if hasattr(dt, 'year') else (int(str(dt)[:4]), int(str(dt)[5:7]))
        if current_month is None:
            current_month = ym
            month_start_nav = val
            logger.debug(f"_calc_monthly_returns: 初始化首月 {ym}")
            continue
        if ym != current_month:
            ret = (val / month_start_nav - 1) if month_start_nav else 0
            monthly.append({
                'year': current_month[0],
                'month': current_month[1],
                'return': round(float(ret), 4),
            })
            current_month = ym
            month_start_nav = val

    if current_month and nav_series.iloc[-1] != month_start_nav:
        ret = (nav_series.iloc[-1] / month_start_nav - 1) if month_start_nav else 0
        monthly.append({
            'year': current_month[0],
            'month': current_month[1],
            'return': round(float(ret), 4),
        })

    return monthly
