"""Polymarket prediction market monitoring API views."""
import logging

from rest_framework.decorators import api_view
from rest_framework.response import Response

from backend.services.polymarket.monitor import get_monitor
from backend.services.polymarket.alert_manager import AlertManager
from backend.services.polymarket.history import PolymarketHistoryDownloader
from backend.services.polymarket.backtester import PolymarketBacktester
from backend.services.polymarket.polymarket_pnl_analyzer import PolymarketPnlAnalyzer
from backend.services.polymarket.a_share_backtester import AShareBacktester
from backend.services.data.database import DatabaseManager
from backend.services.polymarket.models import PolymarketEvent
from backend.tasks.manager import task_manager

logger = logging.getLogger(__name__)

_alert_manager = AlertManager()
_history_downloader = PolymarketHistoryDownloader()
_backtester = PolymarketBacktester()
_pnl_analyzer = PolymarketPnlAnalyzer()
_a_share_backtester = AShareBacktester()
_db = DatabaseManager()


@api_view(['POST'])
def monitor_start(request):
    """启动 Polymarket 监控（提交为后台任务）。"""
    monitor = get_monitor()

    if monitor.is_running:
        return Response({"status": "already_running", "message": "监控已在运行中"})

    task_id = task_manager.submit(
        "Polymarket 监控",
        monitor.start,
    )
    return Response({
        "status": "started",
        "task_id": task_id,
        "message": "Polymarket 监控已启动",
    })


@api_view(['POST'])
def monitor_stop(request):
    """停止 Polymarket 监控。"""
    monitor = get_monitor()

    if not monitor.is_running:
        return Response({"status": "not_running", "message": "监控未在运行"})

    monitor.stop()
    return Response({"status": "stopped", "message": "监控已停止"})


@api_view(['GET'])
def monitor_status(request):
    """获取监控状态 + 监控中的市场列表。"""
    monitor = get_monitor()
    return Response({
        "is_running": monitor.is_running,
        "markets": monitor.monitored_markets,
        "market_count": len(monitor.monitored_markets),
    })


@api_view(['GET'])
def alert_list(request):
    """告警列表（分页，支持 is_read 过滤）。"""
    page = int(request.query_params.get("page", 1))
    page_size = int(request.query_params.get("page_size", 20))
    is_read_param = request.query_params.get("is_read")

    is_read = None
    if is_read_param == "true":
        is_read = True
    elif is_read_param == "false":
        is_read = False

    result = _alert_manager.get_alerts(page=page, page_size=page_size, is_read=is_read)
    return Response(result)


@api_view(['POST'])
def alert_mark_read(request, alert_id):
    """标记告警已读。"""
    success = _alert_manager.mark_read(int(alert_id))
    if success:
        return Response({"status": "ok"})
    return Response({"error": "告警不存在"}, status=404)


@api_view(['POST'])
def mock_alert(request):
    """
    模拟告警测试接口。

    POST body (JSON):
    {
        "question": "Will the US launch military strikes on Iran?",
        "description": "...",                  // 可选
        "category": "politics",                // 可选，默认 politics
        "price_before": 0.12,                  // 必填
        "price_after": 0.67,                   // 必填
        "alert_type": "spike_5m"               // 可选，默认 spike_5m
    }
    """
    data = request.data
    question = data.get("question", "")
    if not question:
        return Response({"error": "question 为必填项"}, status=400)

    price_before = data.get("price_before")
    price_after = data.get("price_after")
    if price_before is None or price_after is None:
        return Response({"error": "price_before 和 price_after 为必填项"}, status=400)

    price_before = float(price_before)
    price_after = float(price_after)
    description = data.get("description", "")
    category = data.get("category", "politics")
    alert_type = data.get("alert_type", "spike_5m")

    timeframe_map = {"spike_5m": 300, "spike_1h": 3600, "spike_24h": 86400}
    timeframe_seconds = timeframe_map.get(alert_type, 300)

    # 用 question hash 作为 condition_id
    import hashlib
    condition_id = "MOCK_" + hashlib.md5(question.encode()).hexdigest()[:12].upper()

    # Upsert mock event
    _db.init_tables()
    session = _db.get_session()
    try:
        from datetime import datetime
        existing = session.query(PolymarketEvent).filter_by(condition_id=condition_id).first()
        if existing:
            existing.outcome_yes_price = price_after
            existing.outcome_no_price = 1.0 - price_after
            existing.is_active = True
        else:
            session.add(PolymarketEvent(
                condition_id=condition_id,
                token_id=f"MOCK_{condition_id}",
                question=question[:1000],
                description=description[:5000] if description else "",
                category=category,
                outcome_yes_price=price_after,
                outcome_no_price=1.0 - price_after,
                volume=0,
                liquidity=0,
                is_active=True,
                slug="mock-event",
                gamma_market_id=f"MOCK_{condition_id}",
            ))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    # 触发告警（在后台线程执行，避免阻塞请求）
    def _run_alert(task_id):
        task_manager.update_progress(task_id, 10, "正在调用 LLM 分析...")
        _alert_manager.trigger_alert(
            condition_id=condition_id,
            alert_type=alert_type,
            market_info={
                "question": question,
                "description": description,
                "category": category,
                "yes_price": price_after,
                "volume": 0,
            },
            price_before=price_before,
            price_after=price_after,
            timeframe_seconds=timeframe_seconds,
        )

    tid = task_manager.submit("模拟告警: " + question[:30], _run_alert)
    return Response({
        "status": "triggered",
        "task_id": tid,
        "condition_id": condition_id,
        "message": f"模拟告警已提交 ({alert_type}: {price_before:.0%} → {price_after:.0%})",
    })


@api_view(['POST'])
def delete_mock_alerts(request):
    """删除所有 Mock 告警及关联的 Mock 事件。"""
    from backend.services.polymarket.models import PolymarketAlert, PolymarketEvent

    _db.init_tables()
    session = _db.get_session()
    try:
        alert_count = session.query(PolymarketAlert).filter(
            PolymarketAlert.condition_id.like("MOCK_%")
        ).delete(synchronize_session=False)

        event_count = session.query(PolymarketEvent).filter(
            PolymarketEvent.condition_id.like("MOCK_%")
        ).delete(synchronize_session=False)

        session.commit()
        return Response({
            "deleted_alerts": alert_count,
            "deleted_events": event_count,
        })
    except Exception as e:
        session.rollback()
        return Response({"error": str(e)}, status=500)
    finally:
        session.close()


# ============================================================
# 回测相关端点
# ============================================================


@api_view(['POST'])
def backtest_discover(request):
    """
    发现已结算的 Polymarket 市场（从 Gamma API 拉取 + 存 DB）。

    POST body (JSON):
    {
        "limit": 50,            // 可选，默认 50
        "min_volume": 50000     // 可选，默认使用 config 值
    }
    """
    data = request.data
    limit = int(data.get("limit", 0))  # 0 = 全部（自动分页）
    min_volume = int(data.get("min_volume", 0))
    exclude_cats = data.get("exclude_categories", ["sports", "pop-culture", "crypto"])
    exclude_set = set(exclude_cats) if exclude_cats else set()

    def _run(task_id):
        return _history_downloader.discover_resolved_markets(
            task_id=task_id,
            limit=limit,
            min_volume=min_volume,
            exclude_categories=exclude_set,
        )

    tid = task_manager.submit("发现已结算市场", _run)
    return Response({"status": "started", "task_id": tid})


@api_view(['POST'])
def backtest_download(request):
    """
    下载已结算市场的历史价格数据。

    POST body (JSON):
    {
        "condition_ids": ["abc123", ...],   // 可选，为空则下载所有已发现的市场
        "limit": 20,                         // 可选，未指定 condition_ids 时自动发现的数量
        "fidelity": 60                       // 可选，数据粒度（分钟），默认 60
    }
    """
    data = request.data
    condition_ids = data.get("condition_ids")
    limit = int(data.get("limit", 0))  # 0 = 全部
    fidelity = int(data.get("fidelity", 60))

    # 如果指定了 condition_ids，从 DB 获取对应市场信息
    markets = None
    if condition_ids:
        session = _db.get_session()
        try:
            events = session.query(PolymarketEvent).filter(
                PolymarketEvent.condition_id.in_(condition_ids)
            ).all()
            markets = [
                {
                    "condition_id": e.condition_id,
                    "token_id": e.token_id,
                    "question": e.question,
                }
                for e in events if e.token_id
            ]
        finally:
            session.close()

    def _run(task_id):
        return _history_downloader.download_batch(
            task_id=task_id,
            markets=markets,
            limit=limit,
            fidelity=fidelity,
        )

    tid = task_manager.submit("下载历史数据", _run)
    return Response({"status": "started", "task_id": tid})


@api_view(['GET'])
def backtest_markets(request):
    """获取已结算市场列表（分页）。"""
    page = int(request.query_params.get("page", 1))
    page_size = int(request.query_params.get("page_size", 20))
    result = _history_downloader.get_resolved_markets(page=page, page_size=page_size)
    return Response(result)


@api_view(['GET'])
def backtest_price_series(request, condition_id):
    """获取单个市场的历史价格序列。"""
    series = _history_downloader.get_price_series(condition_id)
    return Response({"condition_id": condition_id, "series": series})


@api_view(['POST'])
def backtest_backfill_categories(request):
    """从 Gamma API 回填所有 category 为空的事件分类。"""
    result = _history_downloader.backfill_categories()
    return Response(result)


@api_view(['POST'])
def backtest_run(request):
    """
    运行 Polymarket 事件驱动回测。

    POST body (JSON):
    {
        "condition_ids": ["abc123", ...],   // 可选，为空则回测所有已结算市场
        "use_llm": true,                     // 可选，是否调用 LLM 分析
        "spike_5m": 0.05,                   // 可选，自定义阈值
        "spike_1h": 0.15,
        "spike_24h": 0.25,
        "exclude_categories": ["sports", "pop-culture"]  // 已迁移到 is_excluded 字段
    }
    """
    data = request.data
    condition_ids = data.get("condition_ids")
    use_llm = data.get("use_llm", True)
    spike_5m = data.get("spike_5m")
    spike_1h = data.get("spike_1h")
    spike_24h = data.get("spike_24h")
    exclude_categories = data.get("exclude_categories")  # list[str] | None

    if spike_5m is not None:
        spike_5m = float(spike_5m)
    if spike_1h is not None:
        spike_1h = float(spike_1h)
    if spike_24h is not None:
        spike_24h = float(spike_24h)
    if exclude_categories is not None:
        exclude_categories = set(exclude_categories)

    def _run(task_id):
        return _backtester.run(
            task_id=task_id,
            condition_ids=condition_ids,
            use_llm=use_llm,
            spike_5m=spike_5m,
            spike_1h=spike_1h,
            spike_24h=spike_24h,
            exclude_categories=exclude_categories,
        )

    tid = task_manager.submit("Polymarket 回测", _run)
    return Response({"status": "started", "task_id": tid})


@api_view(['GET'])
def backtest_result(request):
    """
    从 DB 重建回测结果（alerts + summary），页面刷新后恢复回测数据。
    """
    import json as _json
    from collections import Counter

    _db.init_tables()
    from backend.services.polymarket.models import PolymarketAlert

    session = _db.get_session()
    try:
        rows = session.query(PolymarketAlert).order_by(
            PolymarketAlert.created_at.desc()
        ).all()

        if not rows:
            return Response({"total_markets": 0, "alerts": [], "summary": {}})

        alerts = []
        for r in rows:
            def _parse_json(val):
                if not val:
                    return []
                try:
                    return _json.loads(val)
                except (ValueError, TypeError):
                    return []

            alerts.append({
                "id": r.id,
                "condition_id": r.condition_id,
                "question": r.question,
                "category": None,
                "alert_type": r.alert_type,
                "price_before": r.price_before,
                "price_after": r.price_after,
                "price_change": r.price_change,
                "timeframe_seconds": r.timeframe_seconds,
                "timestamp": r.created_at.isoformat() if r.created_at else None,
                "affected_tickers": _parse_json(r.affected_tickers),
                "affected_a_shares": _parse_json(r.affected_a_shares),
                "affected_sectors": _parse_json(r.affected_sectors),
                "affected_sw_industries": _parse_json(r.affected_sw_industries),
                "llm_summary": r.llm_summary,
                "llm_sentiment": r.llm_sentiment,
                "llm_confidence": r.llm_confidence,
            })

        # 补充 category
        cid_set = {a["condition_id"] for a in alerts}
        from backend.services.polymarket.models import PolymarketEvent
        events = session.query(PolymarketEvent).filter(
            PolymarketEvent.condition_id.in_(cid_set)
        ).all()
        cat_map = {e.condition_id: e.category for e in events}
        for a in alerts:
            a["category"] = cat_map.get(a["condition_id"])

        # Summary
        total_alerts = len(alerts)
        condition_ids_with_alerts = {a["condition_id"] for a in alerts}

        alert_type_counts = dict(Counter(a["alert_type"] for a in alerts))
        category_counts = dict(Counter(a.get("category") or "unknown" for a in alerts))

        alerts_with_llm = sum(1 for a in alerts if a.get("llm_summary"))
        sentiments = [a["llm_sentiment"] for a in alerts if a.get("llm_sentiment") is not None]
        confidences = [a["llm_confidence"] for a in alerts if a.get("llm_confidence") is not None]

        us_freq: Counter = Counter()
        a_freq: Counter = Counter()
        for a in alerts:
            for t in a.get("affected_tickers") or []:
                tk = t.get("ticker", "")
                if tk:
                    us_freq[tk] += 1
            for s in a.get("affected_a_shares") or []:
                nm = s.get("name", "")
                if nm:
                    a_freq[nm] += 1

        summary = {
            "total_markets": len(condition_ids_with_alerts),
            "markets_with_alerts": len(condition_ids_with_alerts),
            "total_alerts": total_alerts,
            "alert_type_counts": alert_type_counts,
            "category_counts": category_counts,
            "alerts_with_llm": alerts_with_llm,
            "avg_sentiment": round(sum(sentiments) / len(sentiments), 4) if sentiments else None,
            "avg_confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
            "top_us_tickers": [{"ticker": t, "count": c} for t, c in us_freq.most_common(10)],
            "top_a_shares": [{"name": n, "count": c} for n, c in a_freq.most_common(10)],
        }

        # Markets 详情（从 event + snapshot 重建）
        from backend.services.polymarket.models import PolymarketPriceSnapshot
        from sqlalchemy import func

        alerts_per_market = dict(Counter(a["condition_id"] for a in alerts))
        markets = []
        for ev in events:
            cid = ev.condition_id
            if cid not in condition_ids_with_alerts:
                continue
            # 快照统计
            stats = session.query(
                func.count(PolymarketPriceSnapshot.id),
                func.min(PolymarketPriceSnapshot.yes_price),
                func.max(PolymarketPriceSnapshot.yes_price),
                func.min(PolymarketPriceSnapshot.timestamp),
                func.max(PolymarketPriceSnapshot.timestamp),
            ).filter(
                PolymarketPriceSnapshot.condition_id == cid
            ).first()
            n_points, p_min, p_max, t_min, t_max = stats or (0, None, None, None, None)
            # 起止价
            first_snap = session.query(PolymarketPriceSnapshot.yes_price).filter(
                PolymarketPriceSnapshot.condition_id == cid
            ).order_by(PolymarketPriceSnapshot.timestamp.asc()).first()
            last_snap = session.query(PolymarketPriceSnapshot.yes_price).filter(
                PolymarketPriceSnapshot.condition_id == cid
            ).order_by(PolymarketPriceSnapshot.timestamp.desc()).first()

            markets.append({
                "condition_id": cid,
                "question": ev.question,
                "category": ev.category,
                "volume": ev.volume,
                "data_points": n_points or 0,
                "alerts_triggered": alerts_per_market.get(cid, 0),
                "price_start": first_snap[0] if first_snap else None,
                "price_end": last_snap[0] if last_snap else None,
                "price_range": round((p_max or 0) - (p_min or 0), 4) if p_min is not None else 0,
                "time_start": t_min.isoformat() if t_min else None,
                "time_end": t_max.isoformat() if t_max else None,
            })
        markets.sort(key=lambda m: m["volume"] or 0, reverse=True)

        return Response({
            "total_markets": len(condition_ids_with_alerts),
            "markets": markets,
            "alerts": alerts,
            "summary": summary,
        })
    finally:
        session.close()


# ============================================================
# 美股 P&L 回测
# ============================================================


# ============================================================
# 历史影响分析
# ============================================================


@api_view(['GET'])
def impact_overview(request):
    """
    Polymarket 历史影响概览：聚合 alert 数据展示行业/股票影响。

    Query params:
        days (int): 回看天数，默认 365
        min_confidence (float): 最低 LLM 置信度过滤，默认 0.0
    """
    import json
    from datetime import datetime, timedelta
    from collections import Counter

    days = int(request.query_params.get("days", 365))
    min_confidence = float(request.query_params.get("min_confidence", 0.0))

    _db.init_tables()
    from backend.services.polymarket.models import PolymarketAlert

    session = _db.get_session()
    try:
        cutoff = datetime.now() - timedelta(days=days)

        # 基础查询：所有有 LLM 分析的 alert
        query = session.query(PolymarketAlert).filter(
            PolymarketAlert.created_at >= cutoff,
            PolymarketAlert.llm_summary.isnot(None),
        )
        if min_confidence > 0:
            query = query.filter(PolymarketAlert.llm_confidence >= min_confidence)

        alerts = query.order_by(PolymarketAlert.created_at.desc()).all()
        total_alerts = len(alerts)

        if not alerts:
            return Response({
                "summary": {
                    "total_alerts": 0, "alerts_with_llm": 0,
                    "date_range": None, "avg_sentiment": None, "avg_confidence": None,
                    "bridged_articles": 0, "bridged_analysis": 0,
                },
                "industry_impact": [], "stock_impact": [], "daily_timeline": [],
                "category_distribution": {}, "alert_type_distribution": {},
                "recent_alerts": [],
            })

        # 统计
        sentiments = [a.llm_sentiment for a in alerts if a.llm_sentiment is not None]
        confidences = [a.llm_confidence for a in alerts if a.llm_confidence is not None]
        avg_sentiment = round(sum(sentiments) / len(sentiments), 4) if sentiments else None
        avg_confidence = round(sum(confidences) / len(confidences), 4) if confidences else None

        dates = [a.created_at for a in alerts if a.created_at]
        date_range = {
            "earliest": min(dates).strftime("%Y-%m-%d") if dates else None,
            "latest": max(dates).strftime("%Y-%m-%d") if dates else None,
        }

        # 行业影响聚合
        industry_counter: Counter = Counter()
        industry_sentiment: dict[str, list[float]] = {}
        industry_confidence: dict[str, list[float]] = {}

        # 股票影响聚合
        stock_counter: Counter = Counter()
        stock_sentiment: dict[str, list[float]] = {}
        stock_directions: dict[str, Counter] = {}
        stock_names: dict[str, str] = {}

        # 每日时间线
        daily_counter: Counter = Counter()
        daily_sentiment: dict[str, list[float]] = {}

        # 分类/类型分布
        category_counter: Counter = Counter()
        alert_type_counter: Counter = Counter()

        for alert in alerts:
            # 行业
            sw_raw = alert.affected_sw_industries
            if sw_raw:
                try:
                    industries = json.loads(sw_raw) if isinstance(sw_raw, str) else sw_raw
                except (json.JSONDecodeError, TypeError):
                    industries = []
                if isinstance(industries, list):
                    for ind in industries:
                        name = ind if isinstance(ind, str) else str(ind)
                        if not name:
                            continue
                        industry_counter[name] += 1
                        if alert.llm_sentiment is not None:
                            industry_sentiment.setdefault(name, []).append(alert.llm_sentiment)
                        if alert.llm_confidence is not None:
                            industry_confidence.setdefault(name, []).append(alert.llm_confidence)

            # A股
            a_raw = alert.affected_a_shares
            if a_raw:
                try:
                    stocks = json.loads(a_raw) if isinstance(a_raw, str) else a_raw
                except (json.JSONDecodeError, TypeError):
                    stocks = []
                if isinstance(stocks, list):
                    for s in stocks:
                        if not isinstance(s, dict):
                            continue
                        code = s.get("code", "")
                        name = s.get("name", code)
                        direction = s.get("direction", "unknown")
                        if not code:
                            continue
                        stock_counter[code] += 1
                        stock_names[code] = name
                        if alert.llm_sentiment is not None:
                            stock_sentiment.setdefault(code, []).append(alert.llm_sentiment)
                        stock_directions.setdefault(code, Counter())[direction] += 1

            # 每日
            if alert.created_at:
                day_key = alert.created_at.strftime("%Y-%m-%d")
                daily_counter[day_key] += 1
                if alert.llm_sentiment is not None:
                    daily_sentiment.setdefault(day_key, []).append(alert.llm_sentiment)

            # 分类/类型
            # 从 question 推断分类（alert 本身无 category 字段，用 event 关联）
            alert_type_counter[alert.alert_type or "unknown"] += 1

        # 关联 event category
        condition_ids = list({a.condition_id for a in alerts})
        events = session.query(PolymarketEvent).filter(
            PolymarketEvent.condition_id.in_(condition_ids)
        ).all()
        event_category_map = {e.condition_id: e.category or "unknown" for e in events}
        for alert in alerts:
            cat = event_category_map.get(alert.condition_id, "unknown")
            category_counter[cat] += 1

        # 构建行业影响列表
        industry_impact = []
        for name, count in industry_counter.most_common(30):
            sents = industry_sentiment.get(name, [])
            confs = industry_confidence.get(name, [])
            industry_impact.append({
                "industry": name,
                "count": count,
                "avg_sentiment": round(sum(sents) / len(sents), 4) if sents else None,
                "avg_confidence": round(sum(confs) / len(confs), 4) if confs else None,
            })

        # 构建股票影响列表
        stock_impact = []
        for code, count in stock_counter.most_common(50):
            sents = stock_sentiment.get(code, [])
            dirs = stock_directions.get(code, Counter())
            stock_impact.append({
                "code": code,
                "name": stock_names.get(code, code),
                "count": count,
                "avg_sentiment": round(sum(sents) / len(sents), 4) if sents else None,
                "bullish": dirs.get("bullish", 0),
                "bearish": dirs.get("bearish", 0),
            })

        # 每日时间线（按日期排序）
        daily_timeline = []
        for day_key in sorted(daily_counter.keys()):
            sents = daily_sentiment.get(day_key, [])
            daily_timeline.append({
                "date": day_key,
                "count": daily_counter[day_key],
                "avg_sentiment": round(sum(sents) / len(sents), 4) if sents else None,
            })

        # 桥接状态：查询 policy_article 中 source=polymarket 的数量
        bridged_articles = 0
        bridged_analysis = 0
        try:
            df = _db.query("SELECT COUNT(*) as cnt FROM policy_article WHERE source = 'polymarket'")
            bridged_articles = int(df["cnt"].iloc[0]) if not df.empty else 0
            df2 = _db.query(
                "SELECT COUNT(*) as cnt FROM policy_analysis pa "
                "JOIN policy_article a ON pa.article_id = a.id "
                "WHERE a.source = 'polymarket'"
            )
            bridged_analysis = int(df2["cnt"].iloc[0]) if not df2.empty else 0
        except Exception:
            pass

        # 最近的 alert（取最新 20 条，序列化为前端可用格式）
        recent_alerts = []
        for a in alerts[:20]:
            # 解析 JSON 字段
            def _parse_json(val):
                if not val:
                    return []
                if isinstance(val, list):
                    return val
                try:
                    parsed = json.loads(val)
                    return parsed if isinstance(parsed, list) else []
                except (json.JSONDecodeError, TypeError):
                    return []

            recent_alerts.append({
                "id": a.id,
                "condition_id": a.condition_id,
                "alert_type": a.alert_type,
                "question": a.question,
                "price_before": a.price_before,
                "price_after": a.price_after,
                "price_change": a.price_change,
                "llm_summary": a.llm_summary,
                "llm_sentiment": a.llm_sentiment,
                "llm_confidence": a.llm_confidence,
                "affected_sw_industries": _parse_json(a.affected_sw_industries),
                "affected_a_shares": _parse_json(a.affected_a_shares),
                "affected_tickers": _parse_json(a.affected_tickers),
                "affected_sectors": _parse_json(a.affected_sectors),
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "category": event_category_map.get(a.condition_id, "unknown"),
            })

        return Response({
            "summary": {
                "total_alerts": total_alerts,
                "alerts_with_llm": len(sentiments),
                "date_range": date_range,
                "avg_sentiment": avg_sentiment,
                "avg_confidence": avg_confidence,
                "bridged_articles": bridged_articles,
                "bridged_analysis": bridged_analysis,
            },
            "industry_impact": industry_impact,
            "stock_impact": stock_impact,
            "daily_timeline": daily_timeline,
            "category_distribution": dict(category_counter.most_common()),
            "alert_type_distribution": dict(alert_type_counter.most_common()),
            "recent_alerts": recent_alerts,
        })
    finally:
        session.close()


# ============================================================
# 美股 P&L 回测
# ============================================================


@api_view(['POST'])
def us_stock_pnl(request):
    """
    从 alerts JSON 运行美股 P&L 回测。

    POST body (JSON):
    {
        "alerts": [...],                // 告警列表（Polymarket 回测结果的 alerts）
        "holding_days": 5,              // 可选，持仓天数，默认 5
        "min_confidence": 0.0           // 可选，最低置信度，默认 0.0
    }
    """
    data = request.data
    alerts = data.get("alerts")
    if not alerts or not isinstance(alerts, list):
        return Response({"error": "alerts 为必填项且须为列表"}, status=400)

    holding_days = int(data.get("holding_days", 5))
    min_confidence = float(data.get("min_confidence", 0.0))

    def _run(task_id):
        return _pnl_analyzer.run_from_alerts(
            task_id=task_id,
            alerts=alerts,
            holding_days=holding_days,
            min_confidence=min_confidence,
        )

    tid = task_manager.submit("美股 P&L 回测", _run)
    return Response({"status": "started", "task_id": tid})


@api_view(['POST'])
def us_stock_pnl_from_db(request):
    """
    从 DB 告警表运行美股 P&L 回测。

    POST body (JSON):
    {
        "holding_days": 5,              // 可选，持仓天数，默认 5
        "min_confidence": 0.0,          // 可选，最低置信度，默认 0.0
        "limit": 200                    // 可选，告警数量上限，默认 200
    }
    """
    data = request.data
    holding_days = int(data.get("holding_days", 5))
    min_confidence = float(data.get("min_confidence", 0.0))
    limit = int(data.get("limit", 0))

    def _run(task_id):
        return _pnl_analyzer.run_from_db(
            task_id=task_id,
            holding_days=holding_days,
            min_confidence=min_confidence,
            limit=limit,
        )

    tid = task_manager.submit("美股 P&L 回测 (DB)", _run)
    return Response({"status": "started", "task_id": tid})


@api_view(['POST'])
def a_share_pnl_from_db(request):
    """
    从 DB 告警表运行 A 股 P&L 回测。

    POST body (JSON):
    {
        "holding_days": 5,              // 可选，持仓天数，默认 5
        "min_confidence": 0.0,          // 可选，最低置信度，默认 0.0
        "limit": 200                    // 可选，告警数量上限，默认 200
    }
    """
    data = request.data
    holding_days = int(data.get("holding_days", 5))
    min_confidence = float(data.get("min_confidence", 0.0))
    limit = int(data.get("limit", 0))

    def _run(task_id):
        return _a_share_backtester.run_from_db(
            task_id=task_id,
            holding_days=holding_days,
            min_confidence=min_confidence,
            limit=limit,
        )

    tid = task_manager.submit("A股 P&L 回测 (DB)", _run)
    return Response({"status": "started", "task_id": tid})
