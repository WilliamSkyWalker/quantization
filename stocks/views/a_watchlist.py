"""Watchlist (A 股自选股) API views — Django ORM 版。"""
import logging

from django.db.models import Max, OuterRef, Subquery
from rest_framework.decorators import api_view
from rest_framework.response import Response

from stocks.models import ADailyPrice, AStockBasic, AWatchlist

logger = logging.getLogger(__name__)


@api_view(['GET'])
def get_list(request):
    """获取自选股列表，附带最新行情。"""
    wl = list(AWatchlist.objects.order_by("-created_at").values(
        "ts_code", "name", "notes", "created_at",
    ))
    if not wl:
        logger.debug("get_list: 自选股列表为空")
        return Response({'data': []})

    codes = [r["ts_code"] for r in wl]

    # 最新行情：每个 ticker 的 max(trade_date)
    latest_sq = (
        ADailyPrice.objects.filter(ts_code=OuterRef("ts_code"))
        .order_by("-trade_date")
        .values("trade_date")[:1]
    )
    latest_prices = (
        ADailyPrice.objects.filter(ts_code__in=codes, trade_date=Subquery(latest_sq))
        .values("ts_code", "close", "pct_chg", "amount", "trade_date", "total_mv")
    )
    price_map = {}
    for row in latest_prices:
        price_map[row["ts_code"]] = {
            "close": row.get("close"),
            "pct_chg": row.get("pct_chg"),
            "amount": row.get("amount"),
            "trade_date": row["trade_date"].strftime("%Y-%m-%d") if row.get("trade_date") else None,
            "total_mv": row.get("total_mv"),
        }

    result = []
    for row in wl:
        code = row["ts_code"]
        item = {
            "ts_code": code,
            "name": row.get("name") or "",
            "notes": row.get("notes") or "",
            "created_at": row["created_at"].strftime("%Y-%m-%d") if row.get("created_at") else "",
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

    if AWatchlist.objects.filter(ts_code=ts_code).exists():
        return Response({'error': '该股票已在自选股中'}, status=409)

    name = AStockBasic.objects.filter(ts_code=ts_code).values_list("name", flat=True).first() or ""

    try:
        AWatchlist.objects.create(ts_code=ts_code, name=name, notes=notes)
        return Response({'message': f'{ts_code} {name} 已添加到自选股'})
    except Exception as e:
        logger.error(f"添加自选股失败: {e}")
        return Response({'error': str(e)}, status=500)


@api_view(['DELETE'])
def remove(request, ts_code):
    """删除自选股。"""
    deleted, _ = AWatchlist.objects.filter(ts_code=ts_code).delete()
    if deleted:
        return Response({'message': f'{ts_code} 已从自选股移除'})
    return Response({'error': '该股票不在自选股中'}, status=404)


@api_view(['GET'])
def check(request, ts_code):
    """检查某股票是否在自选股中。"""
    exists = AWatchlist.objects.filter(ts_code=ts_code).exists()
    return Response({'in_watchlist': exists})
