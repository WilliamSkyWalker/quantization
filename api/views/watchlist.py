"""Watchlist (自选股) API views."""
import logging

import pandas as pd
from rest_framework.decorators import api_view
from rest_framework.response import Response

from services.data.database import DatabaseManager, Watchlist

logger = logging.getLogger(__name__)


def _get_db():
    db = DatabaseManager()
    db.init_tables()
    return db


@api_view(['GET'])
def get_list(request):
    """获取自选股列表，附带最新行情。"""
    db = _get_db()

    # All watchlist items
    wl = db.query(
        "SELECT ts_code, name, notes, created_at FROM watchlist ORDER BY created_at DESC"
    )
    if wl.empty:
        return Response({'data': []})

    codes = wl['ts_code'].tolist()

    # Latest daily price for each stock
    placeholders = ', '.join([f':c{i}' for i in range(len(codes))])
    params = {f'c{i}': c for i, c in enumerate(codes)}
    price_sql = (
        f"SELECT d.ts_code, d.close, d.pct_chg, d.amount, d.trade_date, d.total_mv "
        f"FROM daily_price d "
        f"INNER JOIN ("
        f"  SELECT ts_code, MAX(trade_date) as max_date "
        f"  FROM daily_price WHERE ts_code IN ({placeholders}) GROUP BY ts_code"
        f") latest ON d.ts_code = latest.ts_code AND d.trade_date = latest.max_date"
    )
    prices = db.query(price_sql, params=params)
    price_map = {}
    if not prices.empty:
        for _, row in prices.iterrows():
            price_map[row['ts_code']] = {
                'close': float(row['close']) if pd.notna(row['close']) else None,
                'pct_chg': float(row['pct_chg']) if pd.notna(row['pct_chg']) else None,
                'amount': float(row['amount']) if pd.notna(row['amount']) else None,
                'trade_date': str(row['trade_date'])[:10] if pd.notna(row['trade_date']) else None,
                'total_mv': float(row['total_mv']) if pd.notna(row['total_mv']) else None,
            }

    result = []
    for _, row in wl.iterrows():
        code = row['ts_code']
        item = {
            'ts_code': code,
            'name': row['name'] or '',
            'notes': row['notes'] or '',
            'created_at': str(row['created_at'])[:10] if pd.notna(row['created_at']) else '',
            **price_map.get(code, {}),
        }
        result.append(item)

    return Response({'data': result})


@api_view(['POST'])
def add(request):
    """添加自选股。"""
    ts_code = request.data.get('ts_code', '').strip()
    notes = request.data.get('notes', '').strip()

    if not ts_code:
        return Response({'error': '请提供股票代码'}, status=400)

    db = _get_db()

    # Check if already exists
    existing = db.query(
        "SELECT id FROM watchlist WHERE ts_code = :code",
        params={'code': ts_code},
    )
    if not existing.empty:
        return Response({'error': '该股票已在自选股中'}, status=409)

    # Get stock name
    basic = db.query(
        "SELECT name FROM stock_basic WHERE ts_code = :code LIMIT 1",
        params={'code': ts_code},
    )
    name = basic.iloc[0]['name'] if not basic.empty else ''

    # Insert
    session = db.SessionLocal()
    try:
        item = Watchlist(ts_code=ts_code, name=name, notes=notes)
        session.add(item)
        session.commit()
        return Response({'message': f'{ts_code} {name} 已添加到自选股'})
    except Exception as e:
        session.rollback()
        logger.error(f"添加自选股失败: {e}")
        return Response({'error': str(e)}, status=500)
    finally:
        session.close()


@api_view(['DELETE'])
def remove(request, ts_code):
    """删除自选股。"""
    db = _get_db()
    session = db.SessionLocal()
    try:
        deleted = session.query(Watchlist).filter(Watchlist.ts_code == ts_code).delete()
        session.commit()
        if deleted:
            return Response({'message': f'{ts_code} 已从自选股移除'})
        return Response({'error': '该股票不在自选股中'}, status=404)
    except Exception as e:
        session.rollback()
        logger.error(f"删除自选股失败: {e}")
        return Response({'error': str(e)}, status=500)
    finally:
        session.close()


@api_view(['GET'])
def check(request, ts_code):
    """检查某股票是否在自选股中。"""
    db = _get_db()
    existing = db.query(
        "SELECT id FROM watchlist WHERE ts_code = :code",
        params={'code': ts_code},
    )
    return Response({'in_watchlist': not existing.empty})
