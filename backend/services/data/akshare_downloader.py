"""
AKShare 数据下载器

通过东方财富 API（免费无积分）下载券商研报等数据。
"""

import logging
from datetime import datetime

import pandas as pd
import requests

from backend.services.config import LOG_LEVEL
from backend.services.data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# 评级映射：中文评级 → 1~5 分
RATING_MAP = {
    "买入": 5.0,
    "增持": 4.0,
    "推荐": 4.0,
    "中性": 3.0,
    "持有": 3.0,
    "审慎增持": 3.5,
    "谨慎推荐": 3.5,
    "减持": 2.0,
    "回避": 2.0,
    "卖出": 1.0,
}

_REPORT_API_URL = "https://reportapi.eastmoney.com/report/list"


def _code_to_ts_code(code: str) -> str:
    """
    将纯数字股票代码转换为 ts_code 格式。

    6 开头 → .SH（上交所），0/3 开头 → .SZ（深交所）。
    """
    code = str(code).strip()
    if len(code) < 6:
        code = code.zfill(6)
    if code.startswith("6"):
        return f"{code}.SH"
    elif code.startswith(("0", "3")):
        return f"{code}.SZ"
    return f"{code}.SZ"  # 默认深交所


def _parse_page_records(items: list) -> list[dict]:
    """将一页 API 原始数据转换为入库记录列表。"""
    if not items:
        return []
    df = pd.DataFrame(items)

    col_map = {
        "stockCode": "code",
        "stockName": "stock_name",
        "orgSName": "institution",
        "researcher": "analyst",
        "title": "title",
        "emRatingName": "rating",
        "publishDate": "report_date",
        "infoCode": "info_code",
    }
    available_cols = {k: v for k, v in col_map.items() if k in df.columns}
    if not available_cols or "stockCode" not in df.columns:
        return []

    df = df.rename(columns=available_cols)
    df["ts_code"] = df["code"].apply(_code_to_ts_code)
    df["rating_score"] = df["rating"].map(RATING_MAP) if "rating" in df.columns else None
    if "report_date" in df.columns:
        df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce").dt.date

    records = []
    for _, row in df.iterrows():
        if pd.isna(row.get("report_date")):
            continue
        records.append({
            "ts_code": row.get("ts_code", ""),
            "stock_name": str(row.get("stock_name", ""))[:50],
            "institution": str(row.get("institution", ""))[:100],
            "analyst": str(row.get("analyst", ""))[:100] if pd.notna(row.get("analyst")) else "",
            "title": str(row.get("title", ""))[:500],
            "rating": str(row.get("rating", ""))[:20] if pd.notna(row.get("rating")) else "",
            "rating_score": float(row["rating_score"]) if pd.notna(row.get("rating_score")) else None,
            "report_date": row["report_date"],
            "info_code": str(row.get("info_code", ""))[:50] if pd.notna(row.get("info_code")) else "",
        })
    return records


class AKShareDownloader:
    """AKShare 数据下载器。"""

    def __init__(self, db: DatabaseManager):
        self.db = db

    def download_research_reports(self, begin_time: str = "2000-01-01", force: bool = False) -> int:
        """
        下载券商研报数据，逐页下载、逐页入库。

        直接调用东方财富研报 API（code 留空 = 全市场）。

        Args:
            begin_time: 起始日期，默认 2000-01-01（全量）。
            force: 跳过提前终止，强制下载全部页。

        Returns:
            新增记录数。
        """
        logger.info(f"开始下载券商研报 (begin_time={begin_time})...")

        end_time = f"{datetime.now().year + 1}-01-01"
        params = {
            "industryCode": "*",
            "pageSize": "5000",
            "industry": "*",
            "rating": "*",
            "ratingChange": "*",
            "beginTime": begin_time,
            "endTime": end_time,
            "pageNo": "1",
            "fields": "",
            "qType": "0",
            "orgCode": "",
            "code": "",
            "rcode": "",
            "p": "1",
            "pageNum": "1",
            "pageNumber": "1",
        }

        # 第一页：获取总页数
        try:
            r = requests.get(_REPORT_API_URL, params=params, timeout=30)
            data_json = r.json()
        except Exception as e:
            logger.error(f"下载券商研报失败: {e}")
            return 0

        total_page = data_json.get("TotalPage", 0)
        if total_page == 0:
            logger.warning("券商研报数据为空")
            return 0

        # API 第 1 页 = 最新数据，最后一页 = 最老数据
        # 增量模式：从第 1 页（最新）正序下载，遇到连续旧数据提前终止
        # force 模式：从第 1 页正序下载全部
        page_range = range(1, total_page + 1)
        if force:
            logger.info(f"研报 API: 共 {total_page} 页，正序全量下载...")
        else:
            logger.info(f"研报 API: 共 {total_page} 页，从第1页正序增量下载...")

        total_new = 0
        no_change_streak = 0
        pages_done = 0

        for page in page_range:
            if not force and no_change_streak >= 3:
                logger.info(f"连续 {no_change_streak} 页无变更，提前终止（已完成 {pages_done}/{total_page} 页）")
                break

            params.update({
                "pageNo": str(page),
                "p": str(page),
                "pageNum": str(page),
                "pageNumber": str(page),
            })
            try:
                r = requests.get(_REPORT_API_URL, params=params, timeout=30)
                page_data = r.json().get("data", [])
            except Exception as e:
                logger.warning(f"研报第 {page} 页下载失败: {e}")
                break

            records = _parse_page_records(page_data)
            pages_done += 1
            if records:
                result = self.db.upsert_research_reports(records)
                total_new += result["new"]
                page_changed = result["new"] + result["updated"]
                no_change_streak = 0 if page_changed > 0 else no_change_streak + 1
            else:
                no_change_streak += 1
            logger.info(f"第 {pages_done}/{total_page} 页 (p={page}): {len(records)} 条, 累计新增 {total_new}")

        logger.info(f"券商研报下载完成: 共新增 {total_new} 条")
        return total_new
