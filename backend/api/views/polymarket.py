"""Polymarket prediction market monitoring API views."""
import logging

from rest_framework.decorators import api_view
from rest_framework.response import Response

from backend.services.polymarket.monitor import get_monitor
from backend.services.polymarket.alert_manager import AlertManager
from backend.services.polymarket.history import PolymarketHistoryDownloader
from backend.services.polymarket.backtester import PolymarketBacktester
from backend.services.data.database import DatabaseManager
from backend.services.polymarket.models import PolymarketEvent
from backend.tasks.manager import task_manager

logger = logging.getLogger(__name__)

_alert_manager = AlertManager()
_history_downloader = PolymarketHistoryDownloader()
_backtester = PolymarketBacktester()
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
    limit = int(data.get("limit", 50))
    min_volume = int(data.get("min_volume", 0))

    def _run(task_id):
        return _history_downloader.discover_resolved_markets(
            task_id=task_id,
            limit=limit,
            min_volume=min_volume,
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
    limit = int(data.get("limit", 20))
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
def backtest_run(request):
    """
    运行 Polymarket 事件驱动回测。

    POST body (JSON):
    {
        "condition_ids": ["abc123", ...],   // 可选，为空则回测所有已结算市场
        "use_llm": true,                     // 可选，是否调用 LLM 分析
        "spike_5m": 0.05,                   // 可选，自定义阈值
        "spike_1h": 0.15,
        "spike_24h": 0.25
    }
    """
    data = request.data
    condition_ids = data.get("condition_ids")
    use_llm = data.get("use_llm", True)
    spike_5m = data.get("spike_5m")
    spike_1h = data.get("spike_1h")
    spike_24h = data.get("spike_24h")

    if spike_5m is not None:
        spike_5m = float(spike_5m)
    if spike_1h is not None:
        spike_1h = float(spike_1h)
    if spike_24h is not None:
        spike_24h = float(spike_24h)

    def _run(task_id):
        return _backtester.run(
            task_id=task_id,
            condition_ids=condition_ids,
            use_llm=use_llm,
            spike_5m=spike_5m,
            spike_1h=spike_1h,
            spike_24h=spike_24h,
        )

    tid = task_manager.submit("Polymarket 回测", _run)
    return Response({"status": "started", "task_id": tid})
