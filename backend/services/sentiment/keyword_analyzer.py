"""
关键词分析器

基于关键词规则的政策文章分析：
    - 行业关键词匹配 → 识别受影响行业
    - 情感关键词匹配 → 判断正面/负面
    - 强度计算 → 结合来源层级和命中数量

零成本、零延迟，作为 LLM 分析的底层。
"""

import logging
import re

from backend.services.config import (
    INDUSTRY_KEYWORDS,
    POSITIVE_KEYWORDS,
    NEGATIVE_KEYWORDS,
    TIER_WEIGHTS,
    LOG_LEVEL,
)

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class KeywordAnalyzer:
    """
    关键词规则分析器。

    对每篇文章执行：
        1. 行业关键词匹配（标题 × 2.0 + 摘要 × 1.0）
        2. 情感关键词匹配（正面 vs 负面）
        3. 强度计算（tier 权重 × 关键词命中密度）

    用法:
        analyzer = KeywordAnalyzer()
        result = analyzer.analyze({
            "title": "...", "summary": "...",
            "source": "gov_cn", "tier": 1,
            "category": "...", "publish_date": "2025-01-15",
        })
    """

    TITLE_WEIGHT = 2.0
    SUMMARY_WEIGHT = 1.0
    CONTENT_WEIGHT = 0.5

    def analyze(self, article: dict) -> dict:
        """
        分析单篇文章。

        Args:
            article: 文章字典，至少包含 title, summary, tier。可选 content。

        Returns:
            {
                "industries": list[str],    # 匹配到的行业列表
                "sentiment": float,         # -1.0 ~ +1.0
                "intensity": float,         # 0.0 ~ 1.0
                "keywords_hit": list[str],  # 命中的关键词
            }
        """
        title = article.get("title", "") or ""
        summary = article.get("summary", "") or ""
        content = article.get("content", "") or ""
        tier = article.get("tier", 5)

        # 1. 行业匹配
        industries, industry_hits = self._match_industries(title, summary, content)

        # 2. 情感匹配
        sentiment, sentiment_hits = self._match_sentiment(title, summary, content)

        # 3. 强度计算
        all_hits = industry_hits + sentiment_hits
        intensity = self._compute_intensity(tier, len(all_hits))

        return {
            "industries": industries,
            "sentiment": sentiment,
            "intensity": intensity,
            "keywords_hit": all_hits,
        }

    def _match_industries(
        self, title: str, summary: str, content: str = "",
    ) -> tuple[list[str], list[str]]:
        """
        匹配行业关键词。

        标题命中权重更高，但行业列表只需去重。
        正文也参与匹配，确保更多行业被发现。

        Returns:
            (匹配到的行业列表, 命中的关键词列表)
        """
        matched_industries = set()
        hit_keywords = []

        for industry, keywords in INDUSTRY_KEYWORDS.items():
            for kw in keywords:
                in_title = kw in title
                in_summary = kw in summary
                in_content = kw in content
                if in_title or in_summary or in_content:
                    matched_industries.add(industry)
                    hit_keywords.append(kw)

        return list(matched_industries), hit_keywords

    def _match_sentiment(
        self, title: str, summary: str, content: str = "",
    ) -> tuple[float, list[str]]:
        """
        匹配情感关键词。

        标题命中权重 × 2.0，摘要命中权重 × 1.0，正文命中权重 × 0.5。
        情感分 = (正面加权命中 - 负面加权命中) / 总加权命中。

        Returns:
            (情感分, 命中的情感关键词列表)
        """
        pos_score = 0.0
        neg_score = 0.0
        hit_keywords = []

        for kw in POSITIVE_KEYWORDS:
            if kw in title:
                pos_score += self.TITLE_WEIGHT
                hit_keywords.append(f"+{kw}")
            if kw in summary:
                pos_score += self.SUMMARY_WEIGHT
                if f"+{kw}" not in hit_keywords:
                    hit_keywords.append(f"+{kw}")
            if kw in content:
                pos_score += self.CONTENT_WEIGHT
                if f"+{kw}" not in hit_keywords:
                    hit_keywords.append(f"+{kw}")

        for kw in NEGATIVE_KEYWORDS:
            if kw in title:
                neg_score += self.TITLE_WEIGHT
                hit_keywords.append(f"-{kw}")
            if kw in summary:
                neg_score += self.SUMMARY_WEIGHT
                if f"-{kw}" not in hit_keywords:
                    hit_keywords.append(f"-{kw}")
            if kw in content:
                neg_score += self.CONTENT_WEIGHT
                if f"-{kw}" not in hit_keywords:
                    hit_keywords.append(f"-{kw}")

        total = pos_score + neg_score
        if total == 0:
            return 0.0, hit_keywords

        sentiment = (pos_score - neg_score) / total
        # 截断到 [-1, 1]
        sentiment = max(-1.0, min(1.0, sentiment))
        return round(sentiment, 4), hit_keywords

    def _compute_intensity(self, tier: int, hit_count: int) -> float:
        """
        计算影响强度。

        强度 = tier 权重 × min(关键词命中数 / 3, 1.0)

        Args:
            tier: 来源层级 (1~5)。
            hit_count: 关键词总命中数。

        Returns:
            强度值 0.0 ~ 1.0。
        """
        tier_w = TIER_WEIGHTS.get(tier, 0.4)
        density = min(hit_count / 3.0, 1.0)
        return round(tier_w * density, 4)
