"""Government Contract Flow 因子

定义：
    GOV_CONTRACT_FLOW = 最近 4 季度政府合同总额 / 市值

    数据源：USGovContract（ticker, year, quarter, amount）

经济直觉：
    - 政府合同 = 稳定现金流 + 进入壁垒
    - 大型政府供应商（国防/医疗/IT）享有预算确定性溢价
    - Cohen et al. (2014): 政府采购增加对公司股价有正向影响
    - 正向因子：高政府合同占比 → 现金流确定性高 → 利好

因子方向：+1
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class GovContractFlow(AlphaSignal):
    """Gov Contract Flow — 政府合同金额 / 市值。"""

    name = "GOV_CONTRACT_FLOW"
    version = "v1"
    category = "alternative"
    horizon = "quarter"
    expected_icir = 0.06
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_gov_contract", "us_enterprise_value"]
    ic_window_months = 24

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("GovContractFlow: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        from stocks.models import USGovContract

        date_ts = pd.Timestamp(date)
        # 最近 4 季度：当前年+上一年的 quarter
        year = date_ts.year
        quarter = (date_ts.month - 1) // 3 + 1

        # 构造最近 4 个 (year, quarter)
        yq_list = []
        y, q = year, quarter
        for _ in range(4):
            yq_list.append((y, q))
            q -= 1
            if q == 0:
                q = 4
                y -= 1

        # Django ORM: OR 条件
        from django.db.models import Q
        q_filter = Q()
        for yy, qq in yq_list:
            q_filter |= Q(year=yy, quarter=qq)

        qs = USGovContract.objects.filter(
            q_filter,
            ticker__in=tickers,
            amount__isnull=False,
            amount__gt=0,
        ).values_list("ticker", "amount")

        df = pd.DataFrame(list(qs), columns=["ticker", "amount"])
        if df.empty:
            logger.warning(f"GovContractFlow({date}): 无政府合同数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
        agg = df.groupby("ticker")["amount"].sum().reset_index()
        agg.columns = ["ticker", "contract_total"]

        # 取市值归一化
        mktcap = self._get_market_cap(date, agg["ticker"].tolist())
        if mktcap.empty:
            logger.warning(f"GovContractFlow({date}): 无市值数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        merged = agg.merge(mktcap, on="ticker", how="inner")
        mc = merged["market_cap"].replace(0, np.nan)
        merged["factor_value"] = merged["contract_total"] / mc

        out = merged[["ticker", "factor_value"]].copy()
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"GovContractFlow({date}): {n_out} / {len(out)} 有值")
        return out

    @staticmethod
    def _get_market_cap(date: str, tickers: list[str]) -> pd.DataFrame:
        from stocks.models import USEnterpriseValue

        date_ts = pd.Timestamp(date)
        start = (date_ts - pd.DateOffset(days=200)).date()

        qs = USEnterpriseValue.objects.filter(
            ticker__in=tickers,
            date__gte=start,
            date__lte=date_ts.date(),
            market_capitalization__isnull=False,
            market_capitalization__gt=0,
        ).values_list("ticker", "date", "market_capitalization")

        df = pd.DataFrame(list(qs), columns=["ticker", "date", "market_cap"])
        if df.empty:
            return pd.DataFrame(columns=["ticker", "market_cap"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df = df.drop_duplicates(subset=["ticker"], keep="first")
        df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
        return df[["ticker", "market_cap"]].reset_index(drop=True)
