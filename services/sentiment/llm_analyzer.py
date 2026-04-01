"""
LLM 分析器（增强层）

对政策文章进行精细语义分析：
    - 识别受影响的申万一级行业
    - 判断情感倾向和影响强度
    - 生成分析摘要

支持两种后端（LLM_PROVIDER 切换）：
    - anthropic: Anthropic Claude API（默认）
    - openai:    OpenAI-compatible API（支持 DeepSeek、通义千问等）

成本控制：仅对 keyword 阶段 intensity >= 阈值的文章调用。
降级策略：无 API key 或调用失败时返回 None，不报错。
"""

import json
import logging
from typing import Optional

from services.config import (
    LLM_PROVIDER,
    LLM_API_KEY,
    LLM_API_BASE,
    LLM_MODEL,
    SENTIMENT_CONTENT_MAX_CHARS,
    LOG_LEVEL,
)

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

SYSTEM_PROMPT = """你是一个专业的A股市场政策分析师。请分析以下政策文章，判断其对申万一级行业和具体A股上市公司的影响。

请返回严格的 JSON 格式：
{
    "industries": ["行业1", "行业2"],
    "stocks": [
        {"code": "600519.SH", "name": "贵州茅台", "impact": 0.8},
        {"code": "000858.SZ", "name": "五粮液", "impact": 0.5}
    ],
    "sentiment": 0.5,
    "intensity": 0.8,
    "impact_type": "industry_regulation",
    "summary": "一句话摘要"
}

字段说明：
- industries: 受影响的申万一级行业列表，必须从以下标准名称中选取（原文原字，不得缩写或修改）：
  银行、非银金融、房地产、建筑装饰、建筑材料、钢铁、有色金属、基础化工、煤炭、石油石化、
  电力设备、公用事业、环保、机械设备、国防军工、电子、计算机、通信、传媒、汽车、
  家用电器、食品饮料、医药生物、农林牧渔、纺织服饰、轻工制造、商贸零售、社会服务、
  交通运输、美容护理、综合
  若无明确相关行业，返回空列表 []
- stocks: 受影响的具体A股上市公司列表。每项包含：
  - code: 股票代码，上交所用 .SH 后缀，深交所用 .SZ 后缀（如 600519.SH、000858.SZ）
  - name: 公司简称
  - impact: 对该股票的影响程度，0.0（几乎无影响）到 1.0（重大直接影响）
  仅列出文章中明确提及或直接相关的公司，不要泛泛列举。若无具体公司，返回空列表 []
- sentiment: 情感倾向，-1.0（强利空）到 +1.0（强利好）
- intensity: 影响强度，0.0（无影响）到 1.0（重大影响）
- impact_type: 政策影响类型，必须从以下 6 种中选取一个：
  - trade_tariff: 贸易关税政策（进出口关税、贸易壁垒、贸易协定）
  - tech_sanction: 技术制裁/出口管制（芯片禁令、实体清单、技术封锁）
  - monetary_policy: 货币政策（利率、准备金率、公开市场操作、汇率）
  - fiscal_stimulus: 财政刺激（减税降费、专项债、补贴、政府投资）
  - industry_regulation: 行业监管（行业准入、合规要求、反垄断、环保标准）
  - general_policy: 一般政策（不属于以上5类的其他政策）
- summary: 50字以内的政策影响摘要

只返回 JSON，不要其他内容。"""

USER_TEMPLATE = """标题：{title}
来源：{source}（层级：{tier}）
分类：{category}
日期：{publish_date}
摘要：{summary}
正文：{content}"""


class LLMAnalyzer:
    """
    LLM 增强分析器。

    根据 LLM_PROVIDER 自动选择后端：
        - "anthropic" → anthropic SDK (messages API)
        - "openai"    → openai SDK (chat completions API)

    缺少 API key 或对应 SDK 未安装时优雅降级。

    用法:
        analyzer = LLMAnalyzer()
        if analyzer.is_available():
            result = analyzer.analyze(article_dict)
    """

    def __init__(self):
        self._client = None
        self._provider = LLM_PROVIDER.lower()
        self._available = False
        self._init_client()

    def _init_client(self):
        """根据 provider 初始化对应客户端。"""
        if not LLM_API_KEY:
            logger.debug("LLM_API_KEY 未配置，LLM 分析器已禁用")
            return

        if self._provider == "anthropic":
            self._init_anthropic()
        else:
            self._init_openai()

    def _init_anthropic(self):
        """初始化 Anthropic Claude 客户端。"""
        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=LLM_API_KEY)
            self._available = True
            logger.info(f"LLM 分析器已启用 (Anthropic): model={LLM_MODEL}")
        except ImportError:
            logger.debug("anthropic 库未安装，LLM 分析器已禁用")
        except Exception as e:
            logger.warning(f"Anthropic 客户端初始化失败: {e}")

    def _init_openai(self):
        """初始化 OpenAI-compatible 客户端。"""
        try:
            from openai import OpenAI
            self._client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_API_BASE)
            self._available = True
            key_preview = LLM_API_KEY[:8] + "..." if len(LLM_API_KEY) > 8 else "***"
            logger.info(
                f"LLM 分析器已启用 (OpenAI-compatible): "
                f"model={LLM_MODEL}, base_url={LLM_API_BASE}, key={key_preview}"
            )
        except ImportError:
            logger.debug("openai 库未安装，LLM 分析器已禁用")
        except Exception as e:
            logger.warning(f"OpenAI 客户端初始化失败: {e}")

    def is_available(self) -> bool:
        """检查 LLM 分析器是否可用。"""
        return self._available

    def analyze(self, article: dict) -> Optional[dict]:
        """
        调用 LLM 分析单篇文章。

        Args:
            article: 文章字典，包含 title, summary, source, tier, category, publish_date。

        Returns:
            {
                "industries": list[str],
                "sentiment": float,
                "intensity": float,
                "summary_text": str,
            }
            失败返回 None。
        """
        if not self._available:
            logger.debug("analyze: LLM 分析器不可用，返回 None")
            return None

        content_raw = str(article.get("content") or "")
        content_truncated = content_raw[:SENTIMENT_CONTENT_MAX_CHARS]

        user_content = USER_TEMPLATE.format(
            title=str(article.get("title") or ""),
            source=str(article.get("source") or ""),
            tier=article.get("tier", ""),
            category=str(article.get("category") or ""),
            publish_date=str(article.get("publish_date") or ""),
            summary=str(article.get("summary") or "")[:500],
            content=content_truncated,
        )

        try:
            logger.info(
                f"[LLM请求] provider={self._provider}, model={LLM_MODEL}, "
                f"base_url={LLM_API_BASE}, article={article.get('title', '')[:50]}"
            )
            if self._provider == "anthropic":
                content = self._call_anthropic(user_content)
            else:
                content = self._call_openai(user_content)
            logger.info(f"[LLM响应] 成功, 长度={len(content)}, 内容={content[:200]}")
            parsed = self._parse_response(content)
            if parsed is None:
                logger.debug(f"analyze: 解析 LLM 响应失败，article={article.get('title', '')[:30]}")
            return parsed
        except Exception as e:
            logger.warning(
                f"[LLM失败] article={article.get('title', '')[:30]}, "
                f"provider={self._provider}, model={LLM_MODEL}, "
                f"base_url={LLM_API_BASE}, error_type={type(e).__name__}, error={e}"
            )
            return None

    def _call_anthropic(self, user_content: str) -> str:
        """调用 Anthropic Messages API。"""
        response = self._client.messages.create(
            model=LLM_MODEL,
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            temperature=0.1,
            timeout=90,
        )
        return response.content[0].text.strip()

    def _call_openai(self, user_content: str) -> str:
        """调用 OpenAI Chat Completions API。"""
        import time
        url = f"{LLM_API_BASE}/chat/completions"
        logger.info(f"[OpenAI请求] POST {url} model={LLM_MODEL} user_content长度={len(user_content)}")
        t0 = time.time()
        response = self._client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0.1,
            max_tokens=500,
            timeout=90,
        )
        elapsed = time.time() - t0
        logger.info(f"[OpenAI响应] 耗时={elapsed:.2f}s, model={response.model}, usage={response.usage}")
        return response.choices[0].message.content.strip()

    def _parse_response(self, content: str) -> Optional[dict]:
        """
        解析 LLM 返回的 JSON。

        容错处理：提取 JSON 部分、校验字段范围。
        """
        # 尝试提取 JSON（可能被 markdown 代码块包裹）
        if "```" in content:
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                content = content[start:end]

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logger.warning(f"LLM 返回非法 JSON: {content[:100]}")
            return None

        # 校验必需字段
        industries = data.get("industries", [])
        if isinstance(industries, str):
            industries = [s.strip() for s in industries.split(",") if s.strip()]

        sentiment = data.get("sentiment", 0.0)
        if not isinstance(sentiment, (int, float)):
            sentiment = 0.0
        sentiment = max(-1.0, min(1.0, float(sentiment)))

        intensity = data.get("intensity", 0.5)
        if not isinstance(intensity, (int, float)):
            intensity = 0.5
        intensity = max(0.0, min(1.0, float(intensity)))

        summary_text = str(data.get("summary", ""))[:2000]

        # 解析受影响的股票
        stocks = data.get("stocks", [])
        if not isinstance(stocks, list):
            stocks = []
        parsed_stocks = []
        for item in stocks:
            if not isinstance(item, dict):
                logger.debug("_parse_response: stocks 中有非 dict 项，跳过")
                continue
            code = str(item.get("code", "")).strip()
            name = str(item.get("name", "")).strip()
            impact = item.get("impact", 0.5)
            if not isinstance(impact, (int, float)):
                impact = 0.5
            impact = max(0.0, min(1.0, float(impact)))
            if code or name:
                parsed_stocks.append({
                    "code": code,
                    "name": name,
                    "impact": round(impact, 4),
                })

        # 解析 impact_type
        valid_impact_types = {
            "trade_tariff", "tech_sanction", "monetary_policy",
            "fiscal_stimulus", "industry_regulation", "general_policy",
        }
        impact_type = str(data.get("impact_type", "general_policy")).strip()
        if impact_type not in valid_impact_types:
            impact_type = "general_policy"

        return {
            "industries": industries,
            "stocks": parsed_stocks,
            "sentiment": round(sentiment, 4),
            "intensity": round(intensity, 4),
            "impact_type": impact_type,
            "summary_text": summary_text,
        }
