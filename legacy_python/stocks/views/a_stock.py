"""A 股 Stock detail API views — Django ORM 版。"""
import logging

import pandas as pd
from django.db import connection
from django.db.models import Max, Q
from rest_framework.decorators import api_view
from rest_framework.response import Response

from stocks.models import (
    ADailyPrice,
    AFinancialIncome,
    AFinancialIndicator,
    AIndustryClass,
    AResearchReport,
    AStockBasic,
)

logger = logging.getLogger(__name__)


@api_view(['GET'])
def search(request):
    """按代码/名称模糊搜索股票（Top 20）。"""
    q = request.query_params.get('q', '').strip()
    if not q or len(q) < 1:
        logger.debug("search: 关键词空")
        return Response({'results': []})

    rows = list(
        AStockBasic.objects.filter(delist_date__isnull=True)
        .filter(Q(ts_code__icontains=q) | Q(name__icontains=q))
        .order_by("ts_code")
        .values("ts_code", "name", "market", "is_st")[:20]
    )
    return Response({'results': rows})


@api_view(['GET'])
def profile(request, ts_code):
    """股票基本信息 + 行业 + 最新财务快照。"""
    basic = AStockBasic.objects.filter(ts_code=ts_code).values(
        "ts_code", "name", "market", "list_date",
        "total_share", "float_share", "is_st",
    ).first()
    if not basic:
        return Response({'error': f'{ts_code} 不存在'}, status=404)

    info = dict(basic)
    if info.get("list_date"):
        info["list_date"] = info["list_date"].strftime("%Y-%m-%d")

    # 行业（申万 L1 + L2）
    ind_l1 = AIndustryClass.objects.filter(
        ts_code=ts_code, src="SW2021", level="L1", out_date__isnull=True,
    ).values_list("index_name", flat=True).first()
    ind_l2 = AIndustryClass.objects.filter(
        ts_code=ts_code, src="SW2021", level="L2", out_date__isnull=True,
    ).values_list("index_name", flat=True).first()
    if ind_l1:
        info["industry_name"] = ind_l1
    if ind_l2:
        info["l2_industry_name"] = ind_l2

    # 最新估值快照（ADailyPrice）
    latest_price = ADailyPrice.objects.filter(ts_code=ts_code).order_by("-trade_date").values(
        "pe_ttm", "pb", "total_mv",
    ).first()
    if latest_price:
        for k in ("pe_ttm", "pb", "total_mv"):
            v = latest_price.get(k)
            if v is not None:
                info[k] = v

    # 最新财报指标（AFinancialIndicator）
    latest_ind = AFinancialIndicator.objects.filter(ts_code=ts_code).order_by("-end_date").values(
        "roe_yearly", "grossprofit_margin",
    ).first()
    if latest_ind:
        info["roe_ttm"] = latest_ind.get("roe_yearly")
        info["gross_margin"] = latest_ind.get("grossprofit_margin")

    # 最新利润表（AFinancialIncome）
    latest_inc = AFinancialIncome.objects.filter(ts_code=ts_code).order_by("-end_date").values(
        "revenue", "n_income_attr_p",
    ).first()
    if latest_inc:
        info["revenue"] = latest_inc.get("revenue")
        info["net_profit"] = latest_inc.get("n_income_attr_p")

    info = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in info.items()}
    return Response(info)


@api_view(['GET'])
def kline(request, ts_code):
    """日线 K 线（前复权 QFQ）。"""
    start_date = request.query_params.get('start_date')
    end_date = request.query_params.get('end_date')

    q = ADailyPrice.objects.filter(ts_code=ts_code)
    if start_date:
        q = q.filter(trade_date__gte=pd.to_datetime(start_date).date())
    if end_date:
        q = q.filter(trade_date__lte=pd.to_datetime(end_date).date())

    rows = list(q.order_by("trade_date").values(
        "trade_date", "open", "high", "low", "close", "vol", "amount", "adj_factor",
    ))
    if not rows:
        logger.debug(f"kline: 无 K 线 {ts_code}")
        return Response({'data': []})

    df = pd.DataFrame(rows).rename(columns={"vol": "volume"})
    latest_adj = float(df["adj_factor"].iloc[-1]) if pd.notna(df["adj_factor"].iloc[-1]) else 1.0
    if latest_adj > 0:
        for col in ("open", "high", "low", "close"):
            df[col] = (df[col] * df["adj_factor"] / latest_adj).round(2)
    df = df.dropna(subset=["open", "high", "low", "close"])

    records = []
    for _, row in df.iterrows():
        records.append({
            "date": row["trade_date"].strftime("%Y-%m-%d"),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]) if pd.notna(row["volume"]) else 0,
            "amount": float(row["amount"]) if pd.notna(row["amount"]) else 0,
        })
    return Response({'data': records})


@api_view(['GET'])
def reports(request, ts_code):
    """研报分页。"""
    page = int(request.query_params.get('page', 1))
    page_size = int(request.query_params.get('page_size', 20))
    offset = (page - 1) * page_size

    q = AResearchReport.objects.filter(ts_code=ts_code)
    total = q.count()
    rows = list(q.order_by("-report_date").values(
        "org_name", "author", "title", "rating", "report_date",
    )[offset:offset + page_size])

    records = [{
        "institution": r.get("org_name") or "",
        "analyst": r.get("author") or "",
        "title": r.get("title") or "",
        "rating": r.get("rating") or "",
        "report_date": r["report_date"].strftime("%Y-%m-%d") if r.get("report_date") else "",
    } for r in rows]
    return Response({'data': records, 'total': total, 'page': page, 'page_size': page_size})


@api_view(['GET'])
def news(request, ts_code):
    """策略相关舆情（policy_article + policy_analysis，raw SQL 直查，Django 无对应 model）。"""
    page = int(request.query_params.get('page', 1))
    page_size = int(request.query_params.get('page_size', 20))
    offset = (page - 1) * page_size

    industry_name = AIndustryClass.objects.filter(
        ts_code=ts_code, src="SW2021", level="L1", out_date__isnull=True,
    ).values_list("index_name", flat=True).first()

    where_parts = ["pa.affected_stocks LIKE %s"]
    params: list = [f'%{ts_code}%']
    if industry_name:
        where_parts.append("pa.industries LIKE %s")
        params.append(f'%{industry_name}%')
    where_clause = " OR ".join(where_parts)

    total = 0
    records = []
    try:
        with connection.cursor() as cur:
            cur.execute(
                f'SELECT COUNT(DISTINCT a.id) FROM policy_article a '
                f'JOIN policy_analysis pa ON pa.article_id = a.id '
                f'WHERE ({where_clause})',
                params,
            )
            total = cur.fetchone()[0]

            cur.execute(
                f'SELECT DISTINCT a.source, a.title, a.publish_date, a.category, '
                f'pa.sentiment, pa.intensity, pa.impact_type, pa.industries '
                f'FROM policy_article a '
                f'JOIN policy_analysis pa ON pa.article_id = a.id '
                f'WHERE ({where_clause}) '
                f'ORDER BY a.publish_date DESC '
                f'LIMIT %s OFFSET %s',
                params + [page_size, offset],
            )
            for row in cur.fetchall():
                source, title, publish_date, category, sentiment, intensity, impact_type, industries = row
                records.append({
                    "source": source or "",
                    "title": title or "",
                    "publish_date": str(publish_date)[:10] if publish_date else "",
                    "category": category or "",
                    "sentiment": round(float(sentiment), 3) if sentiment is not None else None,
                    "intensity": round(float(intensity), 3) if intensity is not None else None,
                    "impact_type": impact_type or "",
                    "industries": industries or "",
                })
    except Exception as e:
        logger.warning(f"news: 查 policy 失败（可能表不存在）: {e}")

    return Response({'data': records, 'total': total, 'page': page, 'page_size': page_size})
