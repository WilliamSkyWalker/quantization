"""
FRED 美股宏观经济数据下载器

负责从 FRED API 获取美国宏观经济指标并存入 MySQL us_macro_indicator 表。
包含 20 个核心指标：GDP、CPI、失业率、联邦基金利率、国债收益率、VIX 等。
"""

import logging
from datetime import datetime
from typing import Optional

import pandas as pd

from services.config import (
    FRED_API_KEY,
    FRED_SERIES_MAP,
    US_DATA_START_DATE,
    LOG_LEVEL,
)
from stocks.models import USMacroIndicator
from stocks.services.upsert import get_upsert_manager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class FREDDownloader:
    """FRED 美股宏观经济数据下载器"""

    def __init__(self, db=None, **kwargs):
        self._um = get_upsert_manager()
        self._start_date = datetime.strptime(US_DATA_START_DATE, "%Y%m%d").strftime("%Y-%m-%d")

        if not FRED_API_KEY:
            logger.warning("FRED_API_KEY 未配置，FRED 数据下载将不可用")
            self.fred = None
        else:
            try:
                from fredapi import Fred
                self.fred = Fred(api_key=FRED_API_KEY)
            except ImportError:
                logger.warning("fredapi 未安装，请运行: pip install fredapi")
                self.fred = None

    def download_all(self, start_date: str = None, end_date: str = None) -> dict[str, int]:
        """
        全量下载所有宏观指标。

        Returns:
            dict: {indicator_code: 下载记录数}
        """
        if not self.fred:
            logger.error("FRED 客户端不可用")
            return {}

        if start_date is None:
            start_date = self._start_date
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        results = {}
        for indicator_code, fred_series in FRED_SERIES_MAP.items():
            try:
                series = self.fred.get_series(
                    fred_series,
                    observation_start=start_date,
                    observation_end=end_date,
                )
                if series is None or series.empty:
                    logger.debug(f"FRED {fred_series} ({indicator_code}): 无数据")
                    results[indicator_code] = 0
                    continue

                # 转为 DataFrame
                df = pd.DataFrame({
                    "indicator_code": indicator_code,
                    "report_date": series.index,
                    "value": series.values,
                })
                # 去除 NaN 值
                df = df.dropna(subset=["value"])

                if not df.empty:
                    self._um.upsert_df(USMacroIndicator, df, ["indicator_code", "report_date"])
                    results[indicator_code] = len(df)
                    logger.debug(f"FRED {indicator_code}: {len(df)} 条")
                else:
                    results[indicator_code] = 0

            except Exception as e:
                logger.warning(f"FRED {indicator_code} ({fred_series}) 下载失败: {e}")
                results[indicator_code] = 0

        total = sum(results.values())
        logger.info(f"FRED 宏观数据下载完成: {total} 条 ({len(results)} 个指标)")
        return results

    def update(self) -> dict[str, int]:
        """增量更新（从 DB 各指标最新日期开始）。"""
        if not self.fred:
            logger.warning("update: FRED 客户端不可用，跳过增量更新")
            return {}

        end_date = datetime.now().strftime("%Y-%m-%d")
        results = {}

        for indicator_code, fred_series in FRED_SERIES_MAP.items():
            try:
                # 查询该指标的最新日期
                try:
                    latest = (
                        USMacroIndicator.objects.filter(indicator_code=indicator_code)
                        .order_by("-report_date")
                        .values_list("report_date", flat=True)
                        .first()
                    )
                    if latest:
                        start = str(latest)
                    else:
                        start = self._start_date
                except Exception as e:
                    logger.debug(f"update: 查询 {indicator_code} 最新日期失败: {e}")
                    start = self._start_date

                series = self.fred.get_series(
                    fred_series,
                    observation_start=start,
                    observation_end=end_date,
                )
                if series is None or series.empty:
                    logger.debug(f"update: {indicator_code} 增量数据为空")
                    results[indicator_code] = 0
                    continue

                df = pd.DataFrame({
                    "indicator_code": indicator_code,
                    "report_date": series.index,
                    "value": series.values,
                })
                df = df.dropna(subset=["value"])

                if not df.empty:
                    self._um.upsert_df(USMacroIndicator, df, ["indicator_code", "report_date"])
                    results[indicator_code] = len(df)
                else:
                    results[indicator_code] = 0

            except Exception as e:
                logger.warning(f"FRED {indicator_code} 增量更新失败: {e}")
                results[indicator_code] = 0

        total = sum(results.values())
        logger.info(f"FRED 宏观数据增量更新完成: {total} 条")
        return results

    def backfill(self) -> dict:
        """补录全量（等同于 download_all）。"""
        return self.download_all()
