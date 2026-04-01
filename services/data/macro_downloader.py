"""
宏观经济数据下载模块

通过 Tushare Pro 接口获取国内和国际宏观经济指标：
    - shibor: 银行间拆借利率（SHIBOR_3M, SHIBOR_ON）
    - shibor_lpr: 贷款市场报价利率（LPR_1Y）
    - cn_cpi: 居民消费价格同比（CPI_YOY）
    - cn_ppi: 工业品出厂价同比（PPI_YOY, PPI_MP_YOY）
    - cn_pmi: 制造业PMI（PMI_MFG, PMI_NEW_ORDER）— 需 2000 积分
    - cn_m: 货币供应同比（M2_YOY, M1_YOY）+ 衍生指标 M1_M2_SPREAD
    - cn_gdp: GDP同比（GDP_YOY）
    - us_tycr: 美国国债收益率（UST_10Y）+ 衍生指标 UST_2Y10Y

数据存入 macro_indicator 表（通用 KV 结构）。
"""

import logging
from datetime import datetime

import pandas as pd
import tushare as ts

from services.config import (
    TUSHARE_TOKEN,
    DATA_START_DATE,
    LOG_LEVEL,
)
from services.data.database import DatabaseManager
from services.data.downloader import TushareRateLimiter, _tushare_call

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class MacroDownloader:
    """
    宏观经济数据下载器。

    用法:
        db = DatabaseManager()
        dl = MacroDownloader(db)
        dl.download_all()       # 全量下载
        dl.update()             # 增量更新
    """

    def __init__(self, db: DatabaseManager):
        self.db = db
        self.pro = ts.pro_api(TUSHARE_TOKEN)
        self.limiter = TushareRateLimiter()

    def download_all(
        self,
        start_date: str = DATA_START_DATE,
        end_date: str | None = None,
    ) -> dict[str, int]:
        """
        全量下载所有宏观指标。

        Args:
            start_date: 起始日期，格式 YYYYMMDD。
            end_date: 结束日期，格式 YYYYMMDD，默认今天。

        Returns:
            {指标名: 记录数} 字典。
        """
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")

        results = {}
        downloaders = [
            ("SHIBOR", self._download_shibor),
            ("LPR", self._download_lpr),
            ("CPI", self._download_cpi),
            ("PPI", self._download_ppi),
            ("PMI", self._download_pmi),
            ("M", self._download_money_supply),
            ("GDP", self._download_gdp),
            ("UST", self._download_us_treasury),
        ]

        for name, func in downloaders:
            try:
                count = func(start_date, end_date)
                results[name] = count
                logger.info(f"宏观数据 {name}: 下载 {count} 条")
            except Exception as e:
                results[name] = 0
                logger.warning(f"宏观数据 {name}: 下载失败 - {e}")

        total = sum(results.values())
        logger.info(f"宏观数据下载完成: {total} 条记录")
        return results

    def backfill(self) -> dict:
        """
        补录宏观数据（全量重下）。

        仅 8 组指标，直接全量重下 DATA_START_DATE ~ 今天。
        等价于从头全量，8 次 API 调用，幂等安全。

        Returns:
            {'total': int, 'detail': dict}
        """
        end_date = datetime.now().strftime("%Y%m%d")
        logger.info(f"宏观数据补录: 全量重下 {DATA_START_DATE} ~ {end_date}")
        results = self.download_all(DATA_START_DATE, end_date)
        return {'total': sum(results.values()), 'detail': results}

    def update(self) -> dict[str, int]:
        """
        增量更新宏观数据，从 DB 最新日期开始。

        Returns:
            {指标名: 记录数} 字典。
        """
        latest = self.db.get_latest_macro_date()
        if latest is None:
            logger.info("宏观数据为空，执行全量下载")
            return self.download_all()

        # 从最新日期前 1 个月开始（覆盖可能的修正数据）
        start = (pd.to_datetime(latest) - pd.DateOffset(months=1)).strftime("%Y%m%d")
        end = datetime.now().strftime("%Y%m%d")
        logger.info(f"宏观数据增量更新: {start} ~ {end}")
        return self.download_all(start, end)

    # ----------------------------------------------------------
    # 私有下载方法
    # ----------------------------------------------------------

    def _download_shibor(self, start_date: str, end_date: str) -> int:
        """下载 SHIBOR 利率（日频）: SHIBOR_3M, SHIBOR_ON。"""
        df = _tushare_call(
            self.pro, "shibor", self.limiter,
            start_date=start_date, end_date=end_date,
        )
        if df.empty:
            logger.debug("_download_shibor: API 返回空数据")
            return 0

        records = []
        for _, row in df.iterrows():
            dt = row["date"]
            if pd.notna(row.get("3m")):
                records.append({"indicator_code": "SHIBOR_3M", "report_date": dt, "value": row["3m"]})
            if pd.notna(row.get("on")):
                records.append({"indicator_code": "SHIBOR_ON", "report_date": dt, "value": row["on"]})

        if records:
            self.db.upsert_macro_indicator(pd.DataFrame(records))
        return len(records)

    def _download_lpr(self, start_date: str, end_date: str) -> int:
        """下载 LPR 利率（日频）: LPR_1Y。"""
        df = _tushare_call(
            self.pro, "shibor_lpr", self.limiter,
            start_date=start_date, end_date=end_date,
        )
        if df.empty:
            logger.debug("_download_lpr: API 返回空数据")
            return 0

        records = []
        for _, row in df.iterrows():
            dt = row["date"]
            if pd.notna(row.get("1y")):
                records.append({"indicator_code": "LPR_1Y", "report_date": dt, "value": row["1y"]})

        if records:
            self.db.upsert_macro_indicator(pd.DataFrame(records))
        return len(records)

    def _download_cpi(self, start_date: str, end_date: str) -> int:
        """下载 CPI 同比（月频）: CPI_YOY。"""
        df = _tushare_call(
            self.pro, "cn_cpi", self.limiter,
            start_month=start_date[:6], end_month=end_date[:6],
            fields="month,nt_yoy",
        )
        if df.empty:
            logger.debug("_download_cpi: API 返回空数据")
            return 0

        records = []
        for _, row in df.iterrows():
            month_str = str(row["month"])
            report_date = pd.to_datetime(month_str + "01") + pd.offsets.MonthEnd(0)
            if pd.notna(row.get("nt_yoy")):
                records.append({
                    "indicator_code": "CPI_YOY",
                    "report_date": report_date,
                    "value": row["nt_yoy"],
                })

        if records:
            self.db.upsert_macro_indicator(pd.DataFrame(records))
        return len(records)

    def _download_ppi(self, start_date: str, end_date: str) -> int:
        """下载 PPI 同比（月频）: PPI_YOY, PPI_MP_YOY。"""
        df = _tushare_call(
            self.pro, "cn_ppi", self.limiter,
            start_month=start_date[:6], end_month=end_date[:6],
            fields="month,ppi_yoy,ppi_mp_yoy",
        )
        if df.empty:
            logger.debug("_download_ppi: API 返回空数据")
            return 0

        records = []
        for _, row in df.iterrows():
            month_str = str(row["month"])
            report_date = pd.to_datetime(month_str + "01") + pd.offsets.MonthEnd(0)
            if pd.notna(row.get("ppi_yoy")):
                records.append({
                    "indicator_code": "PPI_YOY",
                    "report_date": report_date,
                    "value": row["ppi_yoy"],
                })
            if pd.notna(row.get("ppi_mp_yoy")):
                records.append({
                    "indicator_code": "PPI_MP_YOY",
                    "report_date": report_date,
                    "value": row["ppi_mp_yoy"],
                })

        if records:
            self.db.upsert_macro_indicator(pd.DataFrame(records))
        return len(records)

    def _download_pmi(self, start_date: str, end_date: str) -> int:
        """
        下载制造业 PMI（月频）: PMI_MFG, PMI_NEW_ORDER。
        需 2000 积分，获取失败时优雅跳过。
        """
        try:
            df = _tushare_call(
                self.pro, "cn_pmi", self.limiter,
                start_month=start_date[:6], end_month=end_date[:6],
                fields="month,pmi010000,pmi010500",
            )
        except Exception as e:
            logger.info(f"PMI 数据获取跳过（可能积分不足）: {e}")
            return 0

        if df.empty:
            logger.debug("_download_pmi: API 返回空数据")
            return 0

        if "month" not in df.columns:
            logger.warning(f"PMI 返回字段缺少 month 列，实际列: {list(df.columns)}")
            return 0

        records = []
        for _, row in df.iterrows():
            month_str = str(row["month"])
            report_date = pd.to_datetime(month_str + "01") + pd.offsets.MonthEnd(0)
            # pmi010000 = 制造业PMI
            if pd.notna(row.get("pmi010000")):
                records.append({
                    "indicator_code": "PMI_MFG",
                    "report_date": report_date,
                    "value": row["pmi010000"],
                })
            # pmi010500 = 新订单指数
            if pd.notna(row.get("pmi010500")):
                records.append({
                    "indicator_code": "PMI_NEW_ORDER",
                    "report_date": report_date,
                    "value": row["pmi010500"],
                })

        if records:
            self.db.upsert_macro_indicator(pd.DataFrame(records))
        return len(records)

    def _download_money_supply(self, start_date: str, end_date: str) -> int:
        """下载货币供应同比（月频）: M2_YOY, M1_YOY + 衍生 M1_M2_SPREAD。"""
        df = _tushare_call(
            self.pro, "cn_m", self.limiter,
            start_month=start_date[:6], end_month=end_date[:6],
        )
        if df.empty:
            logger.debug("_download_money_supply: API 返回空数据")
            return 0

        records = []
        for _, row in df.iterrows():
            month_str = str(row["month"])
            report_date = pd.to_datetime(month_str + "01") + pd.offsets.MonthEnd(0)
            m2_yoy = row.get("m2_yoy")
            m1_yoy = row.get("m1_yoy")

            if pd.notna(m2_yoy):
                records.append({
                    "indicator_code": "M2_YOY",
                    "report_date": report_date,
                    "value": m2_yoy,
                })
            if pd.notna(m1_yoy):
                records.append({
                    "indicator_code": "M1_YOY",
                    "report_date": report_date,
                    "value": m1_yoy,
                })
            # 衍生指标：M1-M2 剪刀差
            if pd.notna(m1_yoy) and pd.notna(m2_yoy):
                records.append({
                    "indicator_code": "M1_M2_SPREAD",
                    "report_date": report_date,
                    "value": m1_yoy - m2_yoy,
                })

        if records:
            self.db.upsert_macro_indicator(pd.DataFrame(records))
        return len(records)

    def _download_gdp(self, start_date: str, end_date: str) -> int:
        """下载 GDP 同比（季频）: GDP_YOY。"""
        df = _tushare_call(
            self.pro, "cn_gdp", self.limiter,
            start_quarter=_yyyymmdd_to_quarter(start_date),
            end_quarter=_yyyymmdd_to_quarter(end_date),
        )
        if df.empty:
            logger.debug("_download_gdp: API 返回空数据")
            return 0

        records = []
        for _, row in df.iterrows():
            quarter_str = str(row["quarter"])
            report_date = _quarter_to_date(quarter_str)
            if report_date is None:
                logger.debug(f"_download_gdp: 跳过无效季度 {quarter_str}")
                continue
            if pd.notna(row.get("gdp_yoy")):
                records.append({
                    "indicator_code": "GDP_YOY",
                    "report_date": report_date,
                    "value": row["gdp_yoy"],
                })

        if records:
            self.db.upsert_macro_indicator(pd.DataFrame(records))
        return len(records)

    def _download_us_treasury(self, start_date: str, end_date: str) -> int:
        """下载美国国债收益率（日频）: UST_10Y + 衍生 UST_2Y10Y。"""
        df = _tushare_call(
            self.pro, "us_tycr", self.limiter,
            start_date=start_date, end_date=end_date,
        )
        if df.empty:
            logger.debug("_download_us_treasury: API 返回空数据")
            return 0

        records = []
        for _, row in df.iterrows():
            dt = row["date"]
            y10 = row.get("y10")
            y2 = row.get("y2")

            if pd.notna(y10):
                records.append({
                    "indicator_code": "UST_10Y",
                    "report_date": dt,
                    "value": y10,
                })
            # 衍生指标：10Y-2Y 期限利差
            if pd.notna(y10) and pd.notna(y2):
                records.append({
                    "indicator_code": "UST_2Y10Y",
                    "report_date": dt,
                    "value": y10 - y2,
                })

        if records:
            self.db.upsert_macro_indicator(pd.DataFrame(records))
        return len(records)


# ----------------------------------------------------------
# 工具函数
# ----------------------------------------------------------

def _yyyymmdd_to_quarter(date_str: str) -> str:
    """YYYYMMDD → 季度字符串，如 '202501' → '2025Q1'。"""
    year = date_str[:4]
    month = int(date_str[4:6])
    q = (month - 1) // 3 + 1
    return f"{year}Q{q}"


def _quarter_to_date(quarter_str: str) -> str | None:
    """季度字符串 → 季末日期，如 '2025Q1' → '2025-03-31'。"""
    try:
        year = int(quarter_str[:4])
        q = int(quarter_str[-1])
        month = q * 3
        return pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)
    except (ValueError, IndexError) as e:
        logger.debug(f"_quarter_to_date: 解析季度字符串 '{quarter_str}' 失败: {e}")
        return None
