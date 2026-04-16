"""
SimFin 美股历史财报下载器

从 SimFin 批量下载美股季度财报（Income/Balance/Cashflow），
补充 yfinance 只能获取最近 4-8 个季度的限制。

免费版提供 5 年历史（覆盖 ~2020 起），足够回测使用。
需要注册 https://www.simfin.com 获取免费 API Key。

注意：simfin Python 库与 pandas 2.x 不兼容（date_parser 已移除），
因此先用 sf.load_* 触发下载，然后直接用 pd.read_csv 读取 CSV。
"""

import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd

from services.config import (
    SIMFIN_API_KEY,
    SIMFIN_DATA_DIR,
    LOG_LEVEL,
)
from stocks.models import USStockBasic, USFinancialData
from stocks.services.upsert import get_upsert_manager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


def _ensure_downloaded(sf, dataset: str, refresh_days: int):
    """确保 CSV 已下载到本地（绕过 simfin 的 pandas 解析）。"""
    csv_path = Path(SIMFIN_DATA_DIR) / f"{dataset}.csv"
    if csv_path.exists() and refresh_days > 1:
        import time
        age_days = (time.time() - csv_path.stat().st_mtime) / 86400
        if age_days < refresh_days:
            return csv_path

    # 用 simfin 触发下载（会报错但 CSV 已保存）
    try:
        if "income" in dataset:
            sf.load_income(variant="quarterly", market="us", refresh_days=0)
        elif "balance" in dataset:
            sf.load_balance(variant="quarterly", market="us", refresh_days=0)
        elif "cashflow" in dataset:
            sf.load_cashflow(variant="quarterly", market="us", refresh_days=0)
    except Exception as e:
        logger.debug(f"_ensure_downloaded: simfin 加载 {dataset} 抛出异常(预期行为, pandas 2.x 兼容): {e}")

    if csv_path.exists():
        return csv_path
    logger.debug(f"_ensure_downloaded: CSV 文件不存在 {csv_path}")
    return None


def _read_simfin_csv(path: Path) -> pd.DataFrame:
    """读取 SimFin CSV（分号分隔，直接用 pandas）。"""
    df = pd.read_csv(path, sep=";", low_memory=False)
    # 日期列转换
    for col in ["Report Date", "Publish Date", "Restated Date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


class SimFinDownloader:
    """SimFin 美股历史财报下载器。"""

    def __init__(self, db=None, **kwargs):
        self._um = get_upsert_manager()

        if not SIMFIN_API_KEY:
            raise ValueError(
                "SIMFIN_API_KEY 未设置。请到 https://www.simfin.com 注册免费账号，"
                "然后在 .env 中设置 SIMFIN_API_KEY=your_key"
            )

        try:
            import simfin as sf
            sf.set_api_key(SIMFIN_API_KEY)
            os.makedirs(SIMFIN_DATA_DIR, exist_ok=True)
            sf.set_data_dir(SIMFIN_DATA_DIR)
            self._sf = sf
        except ImportError:
            logger.error("simfin 未安装")
            raise ImportError("simfin 未安装，请运行: pip install simfin")

    def download_financials(self, force: bool = False) -> int:
        """
        下载美股季度财报（Income + Balance + Cashflow），upsert 到 us_financial_data。

        Returns:
            写入/更新的记录数。
        """
        our_tickers = set(USStockBasic.objects.filter(is_actively_trading=1).values_list("ticker", flat=True))
        if not our_tickers:
            logger.warning("us_stock_basic 为空，请先下载美股股票列表")
            return 0

        refresh = 0 if force else 30
        logger.info(f"SimFin: 下载季度财报 (force={force})...")

        # 1. 下载 + 读取 CSV
        income_path = _ensure_downloaded(self._sf, "us-income-quarterly", refresh)
        balance_path = _ensure_downloaded(self._sf, "us-balance-quarterly", refresh)
        cashflow_path = _ensure_downloaded(self._sf, "us-cashflow-quarterly", refresh)

        if not income_path or not income_path.exists():
            logger.error("SimFin income CSV 不存在，下载失败")
            return 0

        df_income = _read_simfin_csv(income_path)
        df_income = df_income[df_income["Ticker"].isin(our_tickers)]
        logger.info(f"SimFin income: {len(df_income)} rows after filter")

        if df_income.empty:
            logger.debug("download_financials: income 数据为空，跳过")
            return 0

        df_balance = None
        if balance_path and balance_path.exists():
            df_balance = _read_simfin_csv(balance_path)
            df_balance = df_balance[df_balance["Ticker"].isin(our_tickers)]
            logger.info(f"SimFin balance: {len(df_balance)} rows")

        df_cashflow = None
        if cashflow_path and cashflow_path.exists():
            df_cashflow = _read_simfin_csv(cashflow_path)
            df_cashflow = df_cashflow[df_cashflow["Ticker"].isin(our_tickers)]
            logger.info(f"SimFin cashflow: {len(df_cashflow)} rows")

        # 2. 合并三表
        merge_keys = ["Ticker", "Report Date"]
        df = df_income.copy()

        if df_balance is not None and not df_balance.empty:
            bal_cols = ["Ticker", "Report Date", "Total Assets", "Total Equity",
                        "Total Debt", "Total Current Liabilities"]
            bal_cols = [c for c in bal_cols if c in df_balance.columns]
            df = df.merge(df_balance[bal_cols], on=merge_keys, how="left", suffixes=("", "_bal"))

        if df_cashflow is not None and not df_cashflow.empty:
            cf_cols = ["Ticker", "Report Date", "Net Cash from Operating Activities",
                       "Change in Fixed Assets & Intangibles"]
            cf_cols = [c for c in cf_cols if c in df_cashflow.columns]
            df = df.merge(df_cashflow[cf_cols], on=merge_keys, how="left", suffixes=("", "_cf"))

        # 3. 构建 records
        records = []
        for _, row in df.iterrows():
            ticker = row.get("Ticker")
            report_date = row.get("Report Date")
            publish_date = row.get("Publish Date")
            fiscal_period = row.get("Fiscal Period", "")
            fiscal_year = row.get("Fiscal Year", "")

            if pd.isna(report_date):
                logger.debug(f"download_financials: 跳过记录 (report_date 为空, ticker={ticker})")
                continue

            period = f"{fiscal_period} {fiscal_year}" if fiscal_period and fiscal_year else ""
            if not period:
                rd = pd.to_datetime(report_date)
                q = (rd.month - 1) // 3 + 1
                period = f"Q{q} {rd.year}"

            revenue = _sf(row.get("Revenue"))
            gross_profit = _sf(row.get("Gross Profit"))
            net_income = _sf(row.get("Net Income"))
            operating_income = _sf(row.get("Operating Income (Loss)"))
            shares_diluted = _sf(row.get("Shares (Diluted)"))
            total_assets = _sf(row.get("Total Assets"))
            total_equity = _sf(row.get("Total Equity"))
            total_debt = _sf(row.get("Total Debt"))

            eps = None
            if net_income is not None and shares_diluted and shares_diluted > 0:
                eps = net_income / shares_diluted

            gross_margin = None
            if revenue and gross_profit and abs(revenue) > 1e-6:
                gross_margin = gross_profit / revenue * 100

            operating_margin = None
            if revenue and operating_income and abs(revenue) > 1e-6:
                operating_margin = operating_income / revenue * 100

            roe = None
            if net_income is not None and total_equity and abs(total_equity) > 1e-6:
                roe = net_income / total_equity * 100

            fcf = None
            net_cash_ops = _sf(row.get("Net Cash from Operating Activities"))
            capex = _sf(row.get("Change in Fixed Assets & Intangibles"))
            if net_cash_ops is not None and capex is not None:
                fcf = net_cash_ops + capex  # capex 在 SimFin 中通常为负值

            filing_date = publish_date if pd.notna(publish_date) else report_date

            records.append({
                "ticker": ticker,
                "period": period,
                "date": pd.to_datetime(report_date).strftime("%Y-%m-%d"),
                "filing_date": pd.to_datetime(filing_date).strftime("%Y-%m-%d"),
                "revenue": revenue,
                "net_income": net_income,
                "eps": eps,
                "gross_margin": gross_margin,
                "operating_margin": operating_margin,
                "roe": roe,
                "total_assets": total_assets,
                "total_equity": total_equity,
                "total_debt": total_debt,
                "free_cash_flow": fcf,
                "pe_ratio": None,
                "pb_ratio": None,
            })

        if not records:
            logger.warning("SimFin: 无有效记录")
            return 0

        df_out = pd.DataFrame(records)
        self._um.upsert_df(USFinancialData, df_out, ["ticker", "period"])
        logger.info(
            f"SimFin 财报写入完成: {len(records)} 条 "
            f"({df_out['ticker'].nunique()} 只股票, "
            f"{df_out['date'].min()} ~ {df_out['date'].max()})"
        )
        return len(records)


def _sf(val) -> float | None:
    """Safe float conversion for SimFin values."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None  # NaN/None 无需转换
    try:
        return float(val)
    except (TypeError, ValueError):
        logger.debug(f"_sf: 无法转换值 '{val}' 为 float")
        return None
