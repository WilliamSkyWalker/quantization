"""
模拟 Polymarket 告警：美国打击伊朗

模拟场景：
    - 事件: "Will the US launch military strikes on Iran before July 2026?"
    - 赔率从 12% 在 5 分钟内飙升到 67%（+55%），触发所有三档 Spike
    - 调用 LLM 分析受影响美股
    - 写入 DB + 打印结果
"""

import json
import os
import sys
import django

# Django setup — 项目根目录(quantization/)需要在 sys.path 中
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.core.settings")
django.setup()

from datetime import datetime
from backend.services.data.database import DatabaseManager
from backend.services.polymarket.models import PolymarketEvent, PolymarketAlert
from backend.services.polymarket.alert_manager import AlertManager


def main():
    # 1. 初始化 DB
    db = DatabaseManager()
    db.init_tables()
    session = db.get_session()

    # 2. 插入模拟事件
    condition_id = "MOCK_US_IRAN_STRIKE_2026"
    question = "Will the US launch military strikes on Iran before July 2026?"
    description = (
        "This market resolves YES if the United States conducts direct military strikes "
        "against targets within Iran's sovereign territory before July 1, 2026. "
        "This includes airstrikes, missile strikes, or ground operations carried out by "
        "US military forces. Covert operations, cyberattacks, or strikes against "
        "Iran-backed proxies outside Iran do not count. Resolution is based on official "
        "US government confirmation or credible major news sources (Reuters, AP, BBC)."
    )

    existing = session.query(PolymarketEvent).filter_by(condition_id=condition_id).first()
    if existing:
        existing.outcome_yes_price = 0.67
        existing.outcome_no_price = 0.33
        existing.volume = 2_850_000
        existing.is_active = True
    else:
        event = PolymarketEvent(
            condition_id=condition_id,
            token_id="MOCK_TOKEN_IRAN",
            question=question,
            description=description,
            category="politics",
            outcome_yes_price=0.67,
            outcome_no_price=0.33,
            volume=2_850_000,
            liquidity=450_000,
            end_date=datetime(2026, 7, 1),
            is_active=True,
            slug="will-the-us-launch-military-strikes-on-iran",
            gamma_market_id="MOCK_GAMMA_IRAN",
        )
        session.add(event)
    session.commit()
    print("=" * 70)
    print("MOCK EVENT 已写入 polymarket_event 表")
    print(f"  Question: {question}")
    print(f"  赔率: 12% → 67% (YES)")
    print(f"  Volume: $2,850,000")
    print("=" * 70)

    # 3. 触发告警（模拟 5min Spike: 0.12 → 0.67 = +0.55）
    print("\n触发 AlertManager...")
    alert_mgr = AlertManager()

    market_info = {
        "question": question,
        "description": description,
        "category": "politics",
        "yes_price": 0.67,
        "volume": 2_850_000,
    }

    alert_mgr.trigger_alert(
        condition_id=condition_id,
        alert_type="spike_5m",
        market_info=market_info,
        price_before=0.12,
        price_after=0.67,
        timeframe_seconds=300,
    )

    # 4. 查询刚写入的告警
    session2 = db.get_session()
    alerts = (
        session2.query(PolymarketAlert)
        .filter_by(condition_id=condition_id)
        .order_by(PolymarketAlert.created_at.desc())
        .limit(1)
        .all()
    )

    if not alerts:
        print("\n[!] 未找到告警记录")
        return

    alert = alerts[0]
    print("\n" + "=" * 70)
    print("ALERT 详情 (polymarket_alert 表)")
    print("=" * 70)
    print(f"  ID:             {alert.id}")
    print(f"  Type:           {alert.alert_type}")
    print(f"  Price:          {alert.price_before:.0%} → {alert.price_after:.0%} ({alert.price_change:+.0%})")
    print(f"  Timeframe:      {alert.timeframe_seconds}s")
    print(f"  Question:       {alert.question[:80]}")

    if alert.llm_summary:
        print(f"\n  LLM Summary:    {alert.llm_summary}")
        print(f"  LLM Sentiment:  {alert.llm_sentiment}")
        print(f"  LLM Confidence: {alert.llm_confidence}")
    else:
        print("\n  [LLM 未配置或调用失败，无分析结果]")

    if alert.affected_tickers:
        tickers = json.loads(alert.affected_tickers)
        nq_tag = lambda t: " [NQ100]" if t.get("in_nasdaq100") else ""
        print(f"\n  受影响美股 ({len(tickers)} 只):")
        print(f"  {'Ticker':<10} {'Direction':<10} {'Confidence':<12} Reasoning")
        print(f"  {'-'*10} {'-'*10} {'-'*12} {'-'*40}")
        for t in tickers:
            print(
                f"  {t['ticker'] + nq_tag(t):<10} {t['direction']:<10} {t['confidence']:<12.0%} "
                f"{t.get('reasoning', '')[:50]}"
            )
    else:
        print("\n  [无受影响美股数据]")

    if alert.affected_a_shares:
        a_shares = json.loads(alert.affected_a_shares)
        print(f"\n  受影响A股 ({len(a_shares)} 只):")
        print(f"  {'Code':<12} {'Name':<10} {'Direction':<10} {'Confidence':<12} Reasoning")
        print(f"  {'-'*12} {'-'*10} {'-'*10} {'-'*12} {'-'*40}")
        for s in a_shares:
            print(
                f"  {s['code']:<12} {s['name']:<10} {s['direction']:<10} {s['confidence']:<12.0%} "
                f"{s.get('reasoning', '')[:40]}"
            )
    else:
        print("\n  [无受影响A股数据]")

    if alert.affected_sectors:
        sectors = json.loads(alert.affected_sectors)
        print(f"\n  受影响 GICS 行业: {', '.join(sectors)}")

    if alert.affected_sw_industries:
        sw = json.loads(alert.affected_sw_industries)
        print(f"  受影响申万行业: {', '.join(sw)}")

    print("\n" + "=" * 70)
    session.close()
    session2.close()


if __name__ == "__main__":
    main()
