"""Stock selection and backtest API views."""
import json
import logging
from datetime import date as date_type

import pandas as pd
from rest_framework.decorators import api_view
from rest_framework.response import Response

from backend.services.data.database import DatabaseManager, SelectionResult, FactorSnapshot, BacktestResult
from backend.tasks.manager import task_manager

logger = logging.getLogger(__name__)


def _get_db():
    db = DatabaseManager()
    db.init_tables()
    return db


@api_view(['GET'])
def universe(request):
    """Get tradable stock pool for a given date."""
    from backend.services.data.cleaner import get_clean_universe

    db = _get_db()
    date = request.query_params.get('date')
    if not date:
        date = db.get_latest_trade_date()
    if not date:
        return Response({'error': '无行情数据'}, status=400)

    df = get_clean_universe(db, date)
    if df.empty:
        return Response({'date': date, 'stocks': [], 'total': 0})

    # Get industry distribution
    industry_dist = {}
    if 'industry_name' in df.columns:
        industry_dist = df['industry_name'].value_counts().to_dict()

    limit_up = int(df['is_limit_up'].sum()) if 'is_limit_up' in df.columns else 0
    limit_down = int(df['is_limit_down'].sum()) if 'is_limit_down' in df.columns else 0

    # Convert to list of dicts
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


@api_view(['POST'])
def select_stocks(request):
    """Start async stock selection task."""
    db = _get_db()
    requested_date = request.data.get('date') or request.query_params.get('date')
    latest = db.get_latest_trade_date()
    if not latest:
        return Response({'error': '无行情数据，请先下载行情'}, status=400)

    if not requested_date:
        date = latest
        fallback = False
    else:
        # If requested date is beyond latest available data, fall back
        if requested_date > latest:
            date = latest
            fallback = True
        else:
            date = requested_date
            fallback = False

    task_id = task_manager.submit(
        f'选股 {date}',
        _run_select,
        date,
    )
    return Response({'task_id': task_id, 'date': date, 'fallback': fallback, 'requested_date': requested_date or date})


def _run_select(task_id, date):
    """Execute stock selection in background thread."""
    from backend.services.strategy.multi_factor import MultiFactorStrategy

    db = _get_db()

    task_manager.update_progress(task_id, 10, '计算因子打分...')
    strategy = MultiFactorStrategy(db)
    scored = strategy.score_all_stocks(date, include_factors=True)

    if scored.empty:
        return {'date': date, 'stocks': [], 'total': 0, 'top_stocks': [], 'by_industry': {}}

    task_manager.update_progress(task_id, 80, '整理结果...')

    # Add stock names
    codes_str = "','".join(scored['ts_code'].tolist())
    df_names = db.query(
        f"SELECT ts_code, name FROM stock_basic WHERE ts_code IN ('{codes_str}')"
    )
    scored = scored.merge(df_names, on='ts_code', how='left')

    # Add industry info
    if 'industry_name' not in scored.columns:
        try:
            df_ind = db.query(
                f"SELECT ts_code, industry_name FROM industry_class WHERE ts_code IN ('{codes_str}')"
            )
            scored = scored.merge(df_ind, on='ts_code', how='left')
        except Exception:
            pass

    if 'industry_name' not in scored.columns:
        scored['industry_name'] = '未知'
    scored['industry_name'] = scored['industry_name'].fillna('未知')

    scored = scored.sort_values('score', ascending=False)

    # Add price data (close, pct_chg, amount)
    try:
        df_price = db.query(
            f"SELECT ts_code, close, pct_chg, amount FROM daily_price "
            f"WHERE trade_date = '{date}' AND ts_code IN ('{codes_str}')"
        )
        if not df_price.empty:
            scored = scored.merge(df_price[['ts_code', 'close', 'pct_chg', 'amount']], on='ts_code', how='left')
    except Exception:
        pass

    # Compute score-proportional weights for top N stocks
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

    # Overall top N
    top_n = top_n_df.to_dict('records')

    # Per-industry top 5 (only keep display columns, factor values already in snapshot)
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

    # Clean NaN values for JSON
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

    # Persist factor snapshots (full factor matrix for all stocks)
    _SKIP_COLS = {'ts_code', 'score', 'industry_name', 'name', 'close', 'pct_chg', 'amount', 'weight'}
    factor_cols = [c for c in scored.columns if c not in _SKIP_COLS]
    try:
        with db.get_session() as session:
            session.query(FactorSnapshot).filter(FactorSnapshot.date == date).delete()
            records = []
            for _, row in scored.iterrows():
                fdict = {
                    fc: (None if pd.isna(row[fc]) else float(row[fc]))
                    for fc in factor_cols
                    if fc in row.index
                }
                records.append(FactorSnapshot(
                    date=date,
                    ts_code=row['ts_code'],
                    score=float(row['score']) if pd.notna(row.get('score')) else None,
                    factors=json.dumps(fdict),
                ))
            session.bulk_save_objects(records)
            session.commit()
        logger.info(f'因子快照已保存: {date}, {len(records)} 条')
    except Exception as e:
        logger.warning(f'因子快照保存失败: {e}')

    # Persist to DB
    try:
        with db.get_session() as session:
            existing = session.query(SelectionResult).filter(
                SelectionResult.date == date
            ).first()
            if existing:
                existing.total = result['total']
                existing.top_stocks = json.dumps(result['top_stocks'], ensure_ascii=False)
                existing.by_industry = json.dumps(result['by_industry'], ensure_ascii=False)
            else:
                session.add(SelectionResult(
                    date=date,
                    total=result['total'],
                    top_stocks=json.dumps(result['top_stocks'], ensure_ascii=False),
                    by_industry=json.dumps(result['by_industry'], ensure_ascii=False),
                ))
            session.commit()
    except Exception as e:
        logger.warning(f'选股结果保存失败: {e}')

    return result


@api_view(['GET'])
def select_history(request):
    """Return list of dates that have saved selection results, newest first."""
    db = _get_db()
    rows = db.query(
        "SELECT date, total, updated_at FROM selection_result ORDER BY date DESC"
    )
    dates = []
    for _, r in rows.iterrows():
        d = str(r['date'])[:10]
        dates.append({
            'date': d,
            'total': int(r['total']) if pd.notna(r['total']) else 0,
            'updated_at': str(r['updated_at'])[:19] if pd.notna(r['updated_at']) else None,
        })
    return Response({'dates': dates})


@api_view(['GET'])
def select_history_date(request, date):
    """Return saved selection result for a specific date."""
    db = _get_db()
    rows = db.query(
        f"SELECT * FROM selection_result WHERE date = '{date}' LIMIT 1"
    )
    if rows.empty:
        return Response({'error': f'{date} 无保存的选股结果'}, status=404)
    r = rows.iloc[0]
    return Response({
        'date': str(r['date'])[:10],
        'total': int(r['total']) if pd.notna(r.get('total', None)) else 0,
        'top_stocks': json.loads(r['top_stocks']) if r['top_stocks'] else [],
        'by_industry': json.loads(r['by_industry']) if r['by_industry'] else {},
        'updated_at': str(r['updated_at'])[:19] if pd.notna(r.get('updated_at', None)) else None,
    })


@api_view(['GET'])
def factor_detail(request):
    """Get factor details for a single stock on a given date.

    Queries persisted factor_snapshot first; falls back to full recomputation
    only when the snapshot is not available (e.g. the date was never scored).
    """
    db = _get_db()
    date = request.query_params.get('date')
    code = request.query_params.get('code')

    if not date or not code:
        return Response({'error': '需要 date 和 code 参数'}, status=400)

    # Fast path: query persisted snapshot
    try:
        rows = db.query(
            f"SELECT ts_code, score, factors FROM factor_snapshot "
            f"WHERE date = '{date}' AND ts_code = '{code}' LIMIT 1"
        )
        if not rows.empty:
            r = rows.iloc[0]
            record = {'ts_code': r['ts_code'], 'score': r['score']}
            if r['factors']:
                record.update(json.loads(r['factors']))
            return Response(record)
    except Exception:
        pass

    # Slow path: recompute on demand
    from backend.services.strategy.multi_factor import MultiFactorStrategy

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


@api_view(['POST'])
def backtest_run(request):
    """Start an async backtest."""
    start_date = request.data.get('start_date', '2020-01-01')
    end_date = request.data.get('end_date', '2024-12-31')

    task_id = task_manager.submit(
        f'回测 {start_date}~{end_date}',
        _run_backtest,
        start_date,
        end_date,
    )
    return Response({'task_id': task_id})


def _run_backtest(task_id, start_date, end_date):
    """Execute backtest in background thread."""
    from backend.services.strategy.multi_factor import MultiFactorStrategy
    from backend.services.strategy.backtest import BacktestEngine
    from backend.services.risk.risk_manager import RiskManager

    db = _get_db()

    def ensure_not_cancelled():
        if task_manager.is_cancelled(task_id):
            raise RuntimeError('回测已取消')

    task_manager.update_progress(task_id, 10, '生成选股信号...')
    strategy = MultiFactorStrategy(db)
    ensure_not_cancelled()
    cancel_check = lambda: task_manager.is_cancelled(task_id)
    signals = strategy.generate_signals(start_date, end_date, cancel_check=cancel_check)

    if not signals:
        raise ValueError('无有效信号')

    task_manager.update_progress(task_id, 40, '风控调整...')
    rm = RiskManager(db)
    adjusted = {}
    for dt, df_sig in signals.items():
        ensure_not_cancelled()
        adjusted[dt] = rm.adjust_weights(df_sig, dt)

    # Stock name mapping
    all_codes = set()
    for df_sig in adjusted.values():
        ensure_not_cancelled()
        all_codes.update(df_sig['ts_code'].tolist())
    codes_str = "','".join(all_codes)
    name_map = {}
    if codes_str:
        df_names = db.query(
            f"SELECT ts_code, name FROM stock_basic WHERE ts_code IN ('{codes_str}')"
        )
        name_map = dict(zip(df_names['ts_code'], df_names['name']))

    task_manager.update_progress(task_id, 60, '执行回测...')
    engine = BacktestEngine(db)
    cancel_check = lambda: task_manager.is_cancelled(task_id)
    ensure_not_cancelled()
    result = engine.run(adjusted, start_date, end_date, cancel_check=cancel_check)

    if not result:
        raise ValueError('回测失败')

    task_manager.update_progress(task_id, 80, '计算绩效指标...')
    ensure_not_cancelled()
    summary = engine.summary(result)

    # Industry attribution + latest holdings (from report.py logic)
    task_manager.update_progress(task_id, 88, '行业归因分析...')
    from backend.services.monitor.performance import PerformanceAnalyzer
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

    # Build JSON-serializable result
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

    # Monthly returns
    monthly_data = _calc_monthly_returns(nav) if nav is not None and not nav.empty else []

    # Drawdown series
    drawdown_data = []
    if nav is not None and not nav.empty:
        cummax = nav.cummax()
        dd = (nav - cummax) / cummax
        for dt, v in dd.items():
            drawdown_data.append({
                'date': dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt),
                'drawdown': round(float(v), 4),
            })

    # Summary to dict
    summary_dict = {}
    if summary is not None and not summary.empty:
        for _, row in summary.iterrows():
            summary_dict[row.iloc[0]] = row.iloc[1] if len(row) > 1 else None

    # Signals for display
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

    # Persist to DB (skip signals — large and not displayed in saved view)
    try:
        with db.get_session() as session:
            session.add(BacktestResult(
                start_date=start_date,
                end_date=end_date,
                summary=json.dumps(summary_dict, ensure_ascii=False),
                nav=json.dumps(nav_data, ensure_ascii=False),
                benchmark=json.dumps(benchmark_data, ensure_ascii=False),
                trades=json.dumps(trade_data, ensure_ascii=False),
                monthly=json.dumps(monthly_data, ensure_ascii=False),
                drawdown=json.dumps(drawdown_data, ensure_ascii=False),
                attribution=json.dumps(attr_data, ensure_ascii=False),
                holdings=json.dumps(holdings_data, ensure_ascii=False),
            ))
            session.commit()
        logger.info(f'回测结果已保存: {start_date}~{end_date}')
    except Exception as e:
        logger.warning(f'回测结果保存失败: {e}')

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
    """Return list of saved backtest results, newest first."""
    db = _get_db()
    # 只取前 200 字符提取 headline，避免加载完整 summary JSON
    rows = db.query(
        "SELECT id, start_date, end_date, LEFT(summary, 200) AS summary_head, created_at "
        "FROM backtest_result ORDER BY created_at DESC"
    )
    items = []
    for _, r in rows.iterrows():
        headline = '-'
        head = r.get('summary_head') or ''
        if head:
            try:
                # 尝试从截断的 JSON 中提取总收益
                import re
                m = re.search(r'"总收益":\s*"([^"]*)"', head)
                if m:
                    headline = m.group(1)
            except Exception:
                pass
        items.append({
            'id': int(r['id']),
            'start_date': str(r['start_date'])[:10],
            'end_date': str(r['end_date'])[:10],
            'summary_headline': headline,
            'created_at': str(r['created_at'])[:19] if pd.notna(r['created_at']) else None,
        })
    return Response({'items': items})


@api_view(['GET'])
def backtest_history_detail(request, pk):
    """Return full saved backtest result by id."""
    db = _get_db()
    rows = db.query(
        f"SELECT * FROM backtest_result WHERE id = {int(pk)} LIMIT 1"
    )
    if rows.empty:
        return Response({'error': '未找到该回测记录'}, status=404)
    r = rows.iloc[0]

    def _load(field):
        val = r.get(field)
        if val:
            try:
                return json.loads(val)
            except Exception:
                pass
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
        'attribution': _load('attribution'),
        'holdings': _load('holdings'),
        'created_at': str(r['created_at'])[:19] if pd.notna(r.get('created_at')) else None,
    })


def _calc_monthly_returns(nav_series):
    """Calculate monthly returns from NAV series."""
    monthly = []
    if nav_series is None or nav_series.empty:
        return monthly

    nav_series = nav_series.sort_index()
    # Group by year-month
    prev_nav = nav_series.iloc[0]
    current_month = None

    for dt, val in nav_series.items():
        ym = (dt.year, dt.month) if hasattr(dt, 'year') else (int(str(dt)[:4]), int(str(dt)[5:7]))
        if current_month is None:
            current_month = ym
            month_start_nav = val
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

    # Last month
    if current_month and nav_series.iloc[-1] != month_start_nav:
        ret = (nav_series.iloc[-1] / month_start_nav - 1) if month_start_nav else 0
        monthly.append({
            'year': current_month[0],
            'month': current_month[1],
            'return': round(float(ret), 4),
        })

    return monthly
