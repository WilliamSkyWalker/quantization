"""Stock detail API views."""
import logging

import pandas as pd
from rest_framework.decorators import api_view
from rest_framework.response import Response

from services.data.database import DatabaseManager

logger = logging.getLogger(__name__)


def _get_db():
    db = DatabaseManager()
    db.init_tables()
    return db


@api_view(['GET'])
def search(request):
    """Search stocks by code or name (fuzzy), return top 20."""
    q = request.query_params.get('q', '').strip()
    if not q or len(q) < 1:
        return Response({'results': []})

    db = _get_db()
    df = db.query(
        "SELECT ts_code, name, market, is_st "
        "FROM stock_basic "
        "WHERE delist_date IS NULL "
        "  AND (ts_code LIKE :q OR name LIKE :q) "
        "ORDER BY ts_code LIMIT 20",
        params={'q': f'%{q}%'},
    )
    results = df.to_dict('records') if not df.empty else []
    return Response({'results': results})


@api_view(['GET'])
def profile(request, ts_code):
    """Stock basic info + industry + latest financial data."""
    db = _get_db()

    # Basic info
    basic = db.query(
        "SELECT ts_code, name, market, list_date, total_share, float_share, is_st "
        "FROM stock_basic WHERE ts_code = :code LIMIT 1",
        params={'code': ts_code},
    )
    if basic.empty:
        return Response({'error': f'{ts_code} 不存在'}, status=404)

    info = basic.iloc[0].to_dict()

    # Industry
    ind = db.query(
        "SELECT industry_name, l2_industry_name "
        "FROM industry_class WHERE ts_code = :code LIMIT 1",
        params={'code': ts_code},
    )
    if not ind.empty:
        info['industry_name'] = ind.iloc[0]['industry_name']
        info['l2_industry_name'] = ind.iloc[0]['l2_industry_name']

    # Latest financial data (most recent ann_date)
    fin = db.query(
        "SELECT pe_ttm, pb, roe_ttm, gross_margin, revenue, net_profit, total_mv "
        "FROM financial_data WHERE ts_code = :code "
        "ORDER BY end_date DESC LIMIT 1",
        params={'code': ts_code},
    )
    if not fin.empty:
        for col in fin.columns:
            val = fin.iloc[0][col]
            info[col] = None if pd.isna(val) else val

    # Clean NaN
    info = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in info.items()}

    return Response(info)


@api_view(['GET'])
def kline(request, ts_code):
    """Daily OHLCV with forward adjustment (QFQ)."""
    db = _get_db()
    start_date = request.query_params.get('start_date')
    end_date = request.query_params.get('end_date')

    # Build query with adj_factor for QFQ
    sql = (
        "SELECT trade_date, `open`, high, low, close, volume, amount, adj_factor "
        "FROM daily_price WHERE ts_code = :code"
    )
    params = {'code': ts_code}
    if start_date:
        sql += " AND trade_date >= :start"
        params['start'] = start_date
    if end_date:
        sql += " AND trade_date <= :end"
        params['end'] = end_date
    sql += " ORDER BY trade_date"

    df = db.query(sql, params=params)
    if df.empty:
        return Response({'data': []})

    # QFQ: price * adj_factor / latest_adj_factor
    latest_adj = float(df['adj_factor'].iloc[-1])
    if latest_adj > 0:
        for col in ['open', 'high', 'low', 'close']:
            df[col] = (df[col] * df['adj_factor'] / latest_adj).round(2)

    # Drop rows with NaN in price columns
    df = df.dropna(subset=['open', 'high', 'low', 'close'])

    records = []
    for _, row in df.iterrows():
        records.append({
            'date': str(row['trade_date'])[:10],
            'open': float(row['open']),
            'high': float(row['high']),
            'low': float(row['low']),
            'close': float(row['close']),
            'volume': float(row['volume']) if pd.notna(row['volume']) else 0,
            'amount': float(row['amount']) if pd.notna(row['amount']) else 0,
        })

    return Response({'data': records})


@api_view(['GET'])
def reports(request, ts_code):
    """Research reports for a stock, paginated."""
    db = _get_db()
    page = int(request.query_params.get('page', 1))
    page_size = int(request.query_params.get('page_size', 20))
    offset = (page - 1) * page_size

    # Count
    count_df = db.query(
        "SELECT COUNT(*) as cnt FROM research_report WHERE ts_code = :code",
        params={'code': ts_code},
    )
    total = int(count_df.iloc[0]['cnt']) if not count_df.empty else 0

    # Data
    df = db.query(
        "SELECT institution, analyst, title, rating, report_date "
        "FROM research_report WHERE ts_code = :code "
        "ORDER BY report_date DESC LIMIT :limit OFFSET :offset",
        params={'code': ts_code, 'limit': page_size, 'offset': offset},
    )
    records = []
    for _, row in df.iterrows():
        records.append({
            'institution': row.get('institution', ''),
            'analyst': row.get('analyst', ''),
            'title': row.get('title', ''),
            'rating': row.get('rating', ''),
            'report_date': str(row.get('report_date', ''))[:10],
        })

    return Response({'data': records, 'total': total, 'page': page, 'page_size': page_size})


@api_view(['GET'])
def news(request, ts_code):
    """Policy articles matched by industry or affected_stocks for a stock."""
    db = _get_db()
    page = int(request.query_params.get('page', 1))
    page_size = int(request.query_params.get('page_size', 20))
    offset = (page - 1) * page_size

    # Get the stock's industry
    ind = db.query(
        "SELECT industry_name FROM industry_class WHERE ts_code = :code LIMIT 1",
        params={'code': ts_code},
    )
    industry_name = ind.iloc[0]['industry_name'] if not ind.empty else None

    # Build WHERE clause: match by affected_stocks JSON containing ts_code, or by industry
    conditions = []
    params = {'code': ts_code, 'limit': page_size, 'offset': offset}

    # Match affected_stocks containing the ts_code
    conditions.append("pa.affected_stocks LIKE :stock_pattern")
    params['stock_pattern'] = f'%{ts_code}%'

    if industry_name:
        conditions.append("pa.industries LIKE :ind_pattern")
        params['ind_pattern'] = f'%{industry_name}%'

    where_clause = " OR ".join(conditions)

    # Count
    count_sql = (
        "SELECT COUNT(DISTINCT a.id) as cnt "
        "FROM policy_article a "
        "JOIN policy_analysis pa ON pa.article_id = a.id "
        f"WHERE ({where_clause})"
    )
    count_df = db.query(count_sql, params=params)
    total = int(count_df.iloc[0]['cnt']) if not count_df.empty else 0

    # Data
    data_sql = (
        "SELECT DISTINCT a.id, a.source, a.title, a.publish_date, a.category, "
        "pa.sentiment, pa.intensity, pa.impact_type, pa.industries "
        "FROM policy_article a "
        "JOIN policy_analysis pa ON pa.article_id = a.id "
        f"WHERE ({where_clause}) "
        "ORDER BY a.publish_date DESC "
        "LIMIT :limit OFFSET :offset"
    )
    df = db.query(data_sql, params=params)

    records = []
    for _, row in df.iterrows():
        records.append({
            'source': row.get('source', ''),
            'title': row.get('title', ''),
            'publish_date': str(row.get('publish_date', ''))[:10],
            'category': row.get('category', ''),
            'sentiment': round(float(row['sentiment']), 3) if pd.notna(row.get('sentiment')) else None,
            'intensity': round(float(row['intensity']), 3) if pd.notna(row.get('intensity')) else None,
            'impact_type': row.get('impact_type', ''),
            'industries': row.get('industries', ''),
        })

    return Response({'data': records, 'total': total, 'page': page, 'page_size': page_size})
