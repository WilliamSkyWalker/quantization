"""
舆情分析调度器

协调关键词分析（底层）和 LLM 分析（增强层）：
    1. 查询未分析的文章
    2. 批量关键词分析 → 写入 policy_analysis
    3. 对高强度文章调用 LLM → 写入 policy_analysis（线程池并发）
    4. 计算行业级别每日情感得分（合并 keyword + llm，时间衰减加权）
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import numpy as np
import pandas as pd

from backend.services.config import (
    SENTIMENT_LOOKBACK_DAYS,
    SENTIMENT_DECAY,
    SENTIMENT_LLM_THRESHOLD,
    LOG_LEVEL,
)
from backend.services.data.database import DatabaseManager
from backend.services.sentiment.keyword_analyzer import KeywordAnalyzer
from backend.services.sentiment.llm_analyzer import LLMAnalyzer

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# LLM 并发数（受 API 限速约束，不宜太高）
LLM_CONCURRENCY = 200


class SentimentAnalyzer:
    """
    舆情分析调度器。

    用法:
        db = DatabaseManager()
        analyzer = SentimentAnalyzer(db)
        stats = analyzer.analyze_pending()
        daily = analyzer.get_daily_score("2025-01-31")
    """

    def __init__(self, db: DatabaseManager):
        self.db = db
        self.keyword = KeywordAnalyzer()
        self.llm = LLMAnalyzer()

    def analyze_pending(self, max_articles: int = 500, llm_only: bool = False) -> dict:
        """
        分析所有未分析的文章。

        流程：
            1. 查询未做 keyword 分析的文章 → 批量关键词分析（llm_only=True 时跳过）
            2. 对 keyword intensity >= 阈值且未做 llm 分析的文章 → 调用 LLM

        Args:
            max_articles: 单次最大处理文章数。
            llm_only: 仅执行 LLM 分析，跳过关键词分析阶段。

        Returns:
            {"keyword_analyzed": int, "llm_analyzed": int}
        """
        # 1. Keyword 分析
        keyword_count = 0
        unanalyzed = [] if llm_only else self.db.get_unanalyzed_articles("keyword", limit=max_articles)

        if unanalyzed:
            logger.info(f"待关键词分析: {len(unanalyzed)} 篇")
            records = []
            for article in unanalyzed:
                result = self.keyword.analyze(article)
                records.append({
                    "article_id": article["id"],
                    "analysis_type": "keyword",
                    "industries": ",".join(result["industries"]),
                    "sentiment": result["sentiment"],
                    "intensity": result["intensity"],
                    "impact_type": result.get("impact_type", "general_policy"),
                    "keywords_hit": ",".join(result["keywords_hit"]),
                    "analyzed_at": datetime.now(),
                })

            self.db.upsert_policy_analysis(records)
            keyword_count = len(records)
            logger.info(f"关键词分析完成: {keyword_count} 篇")

        # 2. LLM 分析（仅对高强度文章）
        llm_count = 0
        if self.llm.is_available():
            llm_candidates = self.db.get_unanalyzed_articles("llm", limit=max_articles)
            logger.info(f"未做 LLM 分析的文章: {len(llm_candidates)} 篇")
            if llm_candidates:
                # 筛选 keyword intensity >= 阈值的文章
                high_intensity_ids = self._get_high_intensity_ids(
                    [a["id"] for a in llm_candidates]
                )
                before = len(llm_candidates)
                llm_candidates = [
                    a for a in llm_candidates if a["id"] in high_intensity_ids
                ]
                logger.info(
                    f"intensity >= {SENTIMENT_LLM_THRESHOLD} 筛选: "
                    f"{before} → {len(llm_candidates)} 篇"
                )

                if llm_candidates:
                    logger.info(f"待 LLM 分析: {len(llm_candidates)} 篇 (并发={LLM_CONCURRENCY})")
                    llm_count = self._run_llm_concurrent(llm_candidates)
                    logger.info(f"LLM 分析完成: {llm_count} 篇")
                else:
                    logger.info("无满足 intensity 阈值的文章，跳过 LLM 分析")
        else:
            logger.warning("LLM 分析器不可用，跳过 LLM 阶段")

        return {"keyword_analyzed": keyword_count, "llm_analyzed": llm_count}

    def _analyze_one_llm(self, article: dict) -> dict | None:
        """分析单篇文章并立即写入 DB。供线程池调用。"""
        result = self.llm.analyze(article)
        if result is None:
            return None
        # 序列化受影响股票为 JSON
        stocks = result.get("stocks", [])
        affected_stocks_json = json.dumps(stocks, ensure_ascii=False) if stocks else ""
        record = {
            "article_id": article["id"],
            "analysis_type": "llm",
            "industries": ",".join(result["industries"]),
            "sentiment": result["sentiment"],
            "intensity": result["intensity"],
            "impact_type": result.get("impact_type", "general_policy"),
            "keywords_hit": "",
            "summary_text": result.get("summary_text", ""),
            "affected_stocks": affected_stocks_json,
            "analyzed_at": datetime.now(),
        }
        self.db.upsert_policy_analysis([record])
        return record

    def _run_llm_concurrent(self, articles: list[dict]) -> int:
        """并发执行 LLM 分析，每篇完成立即写入 DB。"""
        count = 0
        with ThreadPoolExecutor(max_workers=LLM_CONCURRENCY) as pool:
            futures = {
                pool.submit(self._analyze_one_llm, article): article
                for article in articles
            }
            for future in as_completed(futures):
                article = futures[future]
                try:
                    result = future.result()
                    if result is not None:
                        count += 1
                        logger.debug(
                            f"LLM 完成: article_id={article['id']} "
                            f"title={article.get('title', '')[:30]}"
                        )
                except Exception as e:
                    logger.warning(
                        f"LLM 异常: article_id={article['id']} error={e}"
                    )
        return count

    def _get_high_intensity_ids(self, article_ids: list[int]) -> set[int]:
        """查询 keyword 分析中 intensity >= 阈值的 article_id。"""
        if not article_ids:
            return set()

        ids_str = ",".join(str(i) for i in article_ids)
        sql = (
            f"SELECT article_id FROM policy_analysis "
            f"WHERE analysis_type = 'keyword' "
            f"AND article_id IN ({ids_str}) "
            f"AND intensity >= {SENTIMENT_LLM_THRESHOLD}"
        )
        df = self.db.query(sql)
        if df.empty:
            return set()
        return set(df["article_id"].tolist())

    def get_daily_score(
        self,
        date: str,
        lookback_days: int | None = None,
    ) -> pd.DataFrame:
        """
        计算某日行业级别情感得分。

        合并 keyword + llm 结果（同一文章 llm 优先），按时间衰减加权。

        Args:
            date: 计算日期，格式 YYYY-MM-DD。
            lookback_days: 回看天数，默认 SENTIMENT_LOOKBACK_DAYS。

        Returns:
            DataFrame[industry_name, sentiment, intensity]
            按行业聚合的加权情感得分。
        """
        if lookback_days is None:
            lookback_days = SENTIMENT_LOOKBACK_DAYS

        df = self.db.get_policy_analysis(date, lookback_days)
        if df.empty:
            return pd.DataFrame(columns=["industry_name", "sentiment", "intensity"])

        # 同一文章 llm 优先：去重保留 llm 行
        df = df.sort_values("analysis_type", ascending=False)  # llm > keyword
        df = df.drop_duplicates(subset=["article_id"], keep="first")

        # 计算时间衰减权重
        ref_date = pd.to_datetime(date)
        df["publish_date"] = pd.to_datetime(df["publish_date"])
        df["days_ago"] = (ref_date - df["publish_date"]).dt.days.clip(lower=0)
        df["time_weight"] = np.exp(-SENTIMENT_DECAY * df["days_ago"])

        # 非标准行业名归一化映射（兼容 LLM 输出的缩写/旧名）
        _INDUSTRY_NORMALIZE = {
            "化工": "基础化工",
            "电力": "公用事业",
            "地产": "房地产",
            "证券": "非银金融",
            "保险": "非银金融",
            "军工": "国防军工",
            "互联网": "计算机",
            "新能源": "电力设备",
            "消费": "食品饮料",
        }

        # 展开行业（一篇文章可能关联多个行业）
        rows = []
        for _, row in df.iterrows():
            industries_str = row.get("industries", "")
            if not industries_str or pd.isna(industries_str):
                continue
            for industry in industries_str.split(","):
                industry = industry.strip()
                if not industry:
                    continue
                # 归一化非标准名
                industry = _INDUSTRY_NORMALIZE.get(industry, industry)
                if industry:
                    rows.append({
                        "industry_name": industry,
                        "sentiment": row["sentiment"] or 0.0,
                        "intensity": row["intensity"] or 0.0,
                        "time_weight": row["time_weight"],
                    })

        if not rows:
            return pd.DataFrame(columns=["industry_name", "sentiment", "intensity"])

        expanded = pd.DataFrame(rows)

        # 按行业聚合：加权平均
        def _weighted_agg(grp):
            w = grp["time_weight"] * grp["intensity"]
            total_w = w.sum()
            if total_w == 0:
                return pd.Series({"sentiment": 0.0, "intensity": 0.0})
            sent = (grp["sentiment"] * w).sum() / total_w
            # 行业强度 = 所有文章强度的加权均值
            inten = (grp["intensity"] * grp["time_weight"]).sum() / grp["time_weight"].sum()
            return pd.Series({"sentiment": round(sent, 4), "intensity": round(inten, 4)})

        result = expanded.groupby("industry_name").apply(_weighted_agg, include_groups=False).reset_index()
        return result

    def get_daily_stock_score(
        self,
        date: str,
        lookback_days: int | None = None,
    ) -> pd.DataFrame:
        """
        计算某日个股级别情感得分（来自 LLM 识别的受影响股票）。

        仅使用 LLM 分析结果中的 affected_stocks 字段，按时间衰减加权。

        Args:
            date: 计算日期，格式 YYYY-MM-DD。
            lookback_days: 回看天数，默认 SENTIMENT_LOOKBACK_DAYS。

        Returns:
            DataFrame[ts_code, sentiment, intensity]
            按个股聚合的加权情感得分。
        """
        if lookback_days is None:
            lookback_days = SENTIMENT_LOOKBACK_DAYS

        df = self.db.get_policy_analysis(date, lookback_days, analysis_type="llm")
        if df.empty:
            return pd.DataFrame(columns=["ts_code", "sentiment", "intensity"])

        # 计算时间衰减权重
        ref_date = pd.to_datetime(date)
        df["publish_date"] = pd.to_datetime(df["publish_date"])
        df["days_ago"] = (ref_date - df["publish_date"]).dt.days.clip(lower=0)
        df["time_weight"] = np.exp(-SENTIMENT_DECAY * df["days_ago"])

        # 展开股票级别数据
        rows = []
        for _, row in df.iterrows():
            affected_str = row.get("affected_stocks", "")
            if not affected_str or pd.isna(affected_str):
                continue
            try:
                stocks = json.loads(affected_str)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(stocks, list):
                continue
            for stock in stocks:
                if not isinstance(stock, dict):
                    continue
                code = str(stock.get("code", "")).strip()
                if not code:
                    continue
                impact = stock.get("impact", 0.5)
                if not isinstance(impact, (int, float)):
                    impact = 0.5
                impact = max(0.0, min(1.0, float(impact)))
                rows.append({
                    "ts_code": code,
                    "sentiment": (row["sentiment"] or 0.0),
                    "intensity": (row["intensity"] or 0.0) * impact,
                    "time_weight": row["time_weight"],
                })

        if not rows:
            return pd.DataFrame(columns=["ts_code", "sentiment", "intensity"])

        expanded = pd.DataFrame(rows)

        # 按个股聚合：加权平均
        def _weighted_agg(grp):
            w = grp["time_weight"] * grp["intensity"]
            total_w = w.sum()
            if total_w == 0:
                return pd.Series({"sentiment": 0.0, "intensity": 0.0})
            sent = (grp["sentiment"] * w).sum() / total_w
            inten = (grp["intensity"] * grp["time_weight"]).sum() / grp["time_weight"].sum()
            return pd.Series({"sentiment": round(sent, 4), "intensity": round(inten, 4)})

        result = expanded.groupby("ts_code").apply(_weighted_agg, include_groups=False).reset_index()
        return result

    def get_analysis_stats(self) -> dict:
        """
        获取分析统计信息。

        Returns:
            {
                "total_articles": int,
                "keyword_analyzed": int,
                "llm_analyzed": int,
                "pending_keyword": int,
                "pending_llm": int,
            }
        """
        try:
            total_df = self.db.query("SELECT COUNT(*) as cnt FROM policy_article")
            total = int(total_df["cnt"].iloc[0])
        except Exception:
            total = 0

        try:
            kw_df = self.db.query(
                "SELECT COUNT(*) as cnt FROM policy_analysis WHERE analysis_type = 'keyword'"
            )
            kw_count = int(kw_df["cnt"].iloc[0])
        except Exception:
            kw_count = 0

        try:
            llm_df = self.db.query(
                "SELECT COUNT(*) as cnt FROM policy_analysis WHERE analysis_type = 'llm'"
            )
            llm_count = int(llm_df["cnt"].iloc[0])
        except Exception:
            llm_count = 0

        return {
            "total_articles": total,
            "keyword_analyzed": kw_count,
            "llm_analyzed": llm_count,
            "pending_keyword": total - kw_count,
            "pending_llm": kw_count - llm_count,  # 已做 keyword 但未做 llm 的
            "llm_available": self.llm.is_available(),
        }
