"""
Polymarket 事件 LLM 分析器

分析预测市场事件对美股和 A 股的影响：
    - 识别受影响的美股 ticker + 方向 + 置信度
    - 识别受影响的 A 股（申万行业 + 个股代码）
    - 生成分析摘要

复用 llm_analyzer.py 的双提供商模式（Anthropic / OpenAI）。
无 API key 时优雅降级。
"""

import json
import logging
from typing import Optional

from backend.services.config import (
    LLM_PROVIDER,
    LLM_API_KEY,
    LLM_API_BASE,
    LLM_MODEL,
    US_FALLBACK_TICKERS,
    LOG_LEVEL,
)

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

SYSTEM_PROMPT = """你是一位专业的全球股票市场分析师，擅长预测市场和事件驱动交易。请分析 Polymarket 预测市场事件及其赔率变动对美股和A股的影响。

请返回严格的 JSON 格式（所有文本字段请使用中文）：
{
    "affected_tickers": [
        {"ticker": "LMT", "direction": "bullish", "confidence": 0.9, "reasoning": "国防开支增加的直接受益者"}
    ],
    "affected_a_shares": [
        {"code": "601857.SH", "name": "中国石油", "direction": "bullish", "confidence": 0.8, "reasoning": "国际油价暴涨利好上游勘探开发业务"}
    ],
    "affected_sectors": ["能源", "工业"],
    "affected_sw_industries": ["石油石化", "有色金属", "国防军工"],
    "summary": "2-3句话的中文分析摘要，覆盖美股和A股的影响",
    "overall_sentiment": -0.6,
    "confidence": 0.75
}

字段说明：
- affected_tickers: 受影响的美股列表。每项包含：
  - ticker: 美股代码（如 AAPL、LMT、XOM、TSLA）
  - direction: "bullish"（利好）或 "bearish"（利空）
  - confidence: 0.0 ~ 1.0 置信度
  - reasoning: 中文简要理由
  包含所有有明确直接敞口的美国上市股票，不限于纳斯达克100。最多10只。
- affected_a_shares: 受影响的A股列表。每项包含：
  - code: A股代码（上交所用 .SH 后缀，深交所用 .SZ 后缀，如 600519.SH、000858.SZ）
  - name: 公司简称
  - direction: "bullish"（利好）或 "bearish"（利空）
  - confidence: 0.0 ~ 1.0 置信度
  - reasoning: 中文简要理由
  仅列出通过供应链、大宗商品价格或地缘政治存在明确联动关系的股票。最多10只。
- affected_sectors: 受影响的 GICS 行业（中文名称，如"能源"、"工业"、"信息技术"、"医疗保健"等）
- affected_sw_industries: 受影响的申万一级行业，必须使用以下标准名称：
  银行、非银金融、房地产、建筑装饰、建筑材料、钢铁、有色金属、基础化工、煤炭、石油石化、
  电力设备、公用事业、环保、机械设备、国防军工、电子、计算机、通信、传媒、汽车、
  家用电器、食品饮料、医药生物、农林牧渔、纺织服饰、轻工制造、商贸零售、社会服务、
  交通运输、美容护理、综合
- summary: 2-3句话的中文分析摘要，覆盖美股和A股市场的影响
- overall_sentiment: -1.0（强利空）到 +1.0（强利好）
- confidence: 0.0 ~ 1.0 整体分析置信度

只返回 JSON，不要其他内容。"""

USER_TEMPLATE = """Polymarket 预测市场事件分析：

事件问题: {question}
事件描述: {description}
分类: {category}

赔率变动:
- 变动前: {price_before:.1%}（YES 概率）
- 变动后: {price_after:.1%}（YES 概率）
- 变动幅度: {price_change:+.1%}，时间跨度: {timeframe}
- 告警类型: {alert_type}

当前 YES 赔率 {price_after:.0%} 意味着市场认为该事件发生的概率为 {price_after:.0%}。

请分析哪些美股和A股会受到该事件及其赔率变动的影响最大，并给出买卖方向建议。"""


class EventAnalyzer:
    """
    Polymarket 事件 LLM 分析器。

    复用 Anthropic / OpenAI 双提供商模式。
    无 API key 或 SDK 未安装时优雅降级。
    """

    def __init__(self):
        self._client = None
        self._provider = LLM_PROVIDER.lower()
        self._available = False
        self._nasdaq100 = set(US_FALLBACK_TICKERS)
        self._init_client()

    def _init_client(self):
        if not LLM_API_KEY:
            logger.debug("LLM_API_KEY 未配置，EventAnalyzer 已禁用")
            return

        if self._provider == "anthropic":
            self._init_anthropic()
        else:
            self._init_openai()

    def _init_anthropic(self):
        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=LLM_API_KEY)
            self._available = True
            logger.info(f"EventAnalyzer 已启用 (Anthropic): model={LLM_MODEL}")
        except ImportError:
            logger.debug("anthropic 库未安装，EventAnalyzer 已禁用")
        except Exception as e:
            logger.warning(f"Anthropic 初始化失败: {e}")

    def _init_openai(self):
        try:
            from openai import OpenAI
            self._client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_API_BASE)
            self._available = True
            logger.info(f"EventAnalyzer 已启用 (OpenAI): model={LLM_MODEL}")
        except ImportError:
            logger.debug("openai 库未安装，EventAnalyzer 已禁用")
        except Exception as e:
            logger.warning(f"OpenAI 初始化失败: {e}")

    def is_available(self) -> bool:
        return self._available

    def analyze(self, event_info: dict) -> Optional[dict]:
        """
        分析事件对美股和 A 股的影响。

        Returns:
            {
                "affected_tickers": [{"ticker", "direction", "confidence", "reasoning", "in_nasdaq100"}],
                "affected_a_shares": [{"code", "name", "direction", "confidence", "reasoning"}],
                "affected_sectors": [str],
                "affected_sw_industries": [str],
                "summary": str,
                "overall_sentiment": float,
                "confidence": float,
            }
            失败返回 None。
        """
        if not self._available:
            return None

        timeframe_map = {
            "spike_5m": "5分钟",
            "spike_1h": "1小时",
            "spike_24h": "24小时",
        }
        timeframe = timeframe_map.get(
            event_info.get("alert_type", ""),
            f"{event_info.get('timeframe_seconds', 0)}s"
        )

        user_content = USER_TEMPLATE.format(
            question=event_info.get("question", ""),
            description=(event_info.get("description") or "")[:3000],
            category=event_info.get("category", ""),
            price_before=event_info.get("price_before", 0.5),
            price_after=event_info.get("price_after", 0.5),
            price_change=event_info.get("price_change", 0),
            timeframe=timeframe,
            alert_type=event_info.get("alert_type", ""),
        )

        try:
            if self._provider == "anthropic":
                content = self._call_anthropic(user_content)
            else:
                content = self._call_openai(user_content)
            return self._parse_response(content)
        except Exception as e:
            logger.warning(f"EventAnalyzer 调用失败: {e}")
            return None

    def _call_anthropic(self, user_content: str) -> str:
        response = self._client.messages.create(
            model=LLM_MODEL,
            max_tokens=1200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            temperature=0.1,
            timeout=90,
        )
        return response.content[0].text.strip()

    def _call_openai(self, user_content: str) -> str:
        response = self._client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0.1,
            max_tokens=1200,
            timeout=90,
        )
        return response.choices[0].message.content.strip()

    def _parse_response(self, content: str) -> Optional[dict]:
        """解析 LLM JSON 响应。"""
        if "```" in content:
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                content = content[start:end]

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logger.warning(f"LLM 返回非法 JSON: {content[:200]}")
            return None

        # --- 解析美股 tickers（不再硬过滤，只标记是否在 NASDAQ 100 中）---
        raw_tickers = data.get("affected_tickers", [])
        if not isinstance(raw_tickers, list):
            raw_tickers = []

        us_tickers = []
        for item in raw_tickers:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker", "")).upper().strip()
            if not ticker:
                continue
            direction = str(item.get("direction", "")).lower()
            if direction not in ("bullish", "bearish"):
                direction = "bearish" if data.get("overall_sentiment", 0) < 0 else "bullish"
            confidence = self._clamp(item.get("confidence", 0.5))
            us_tickers.append({
                "ticker": ticker,
                "direction": direction,
                "confidence": round(confidence, 2),
                "reasoning": str(item.get("reasoning", ""))[:500],
                "in_nasdaq100": ticker in self._nasdaq100,
            })

        # --- 解析 A 股 ---
        raw_a_shares = data.get("affected_a_shares", [])
        if not isinstance(raw_a_shares, list):
            raw_a_shares = []

        a_shares = []
        for item in raw_a_shares:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code", "")).strip()
            name = str(item.get("name", "")).strip()
            if not code and not name:
                continue
            direction = str(item.get("direction", "")).lower()
            if direction not in ("bullish", "bearish"):
                direction = "bearish" if data.get("overall_sentiment", 0) < 0 else "bullish"
            confidence = self._clamp(item.get("confidence", 0.5))
            a_shares.append({
                "code": code,
                "name": name,
                "direction": direction,
                "confidence": round(confidence, 2),
                "reasoning": str(item.get("reasoning", ""))[:500],
            })

        # --- 解析行业 ---
        sectors = data.get("affected_sectors", [])
        if not isinstance(sectors, list):
            sectors = []
        sectors = [str(s) for s in sectors]

        sw_industries = data.get("affected_sw_industries", [])
        if not isinstance(sw_industries, list):
            sw_industries = []
        sw_industries = [str(s) for s in sw_industries]

        # --- 解析摘要和情感 ---
        summary = str(data.get("summary", ""))[:2000]

        sentiment = data.get("overall_sentiment", 0.0)
        if not isinstance(sentiment, (int, float)):
            sentiment = 0.0
        sentiment = max(-1.0, min(1.0, float(sentiment)))

        confidence = self._clamp(data.get("confidence", 0.5))

        return {
            "affected_tickers": us_tickers,
            "affected_a_shares": a_shares,
            "affected_sectors": sectors,
            "affected_sw_industries": sw_industries,
            "summary": summary,
            "overall_sentiment": round(sentiment, 4),
            "confidence": round(confidence, 4),
        }

    @staticmethod
    def _clamp(val, lo=0.0, hi=1.0) -> float:
        if not isinstance(val, (int, float)):
            return 0.5
        return max(lo, min(hi, float(val)))
