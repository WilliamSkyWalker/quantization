"""Residual Momentum (Blitz, Huij, Martens 2011, JEF)

    R_it = α_i + β₁·(Mkt-RF)_t + β₂·SMB_t + β₃·HML_t + ε_it

    FACTOR_it = cumsum(ε over past 12-1 months)

剔除 FF3 因子暴露后的残差动量。比纯 12-1 动量：
    - 更稳定（剔除风格暴露 → 熊市/牛市风格切换时不被打爆）
    - IC 更持续（不依赖风格红利）

实现（向量化）：
    1. Pivot 成 wide-format 收益率矩阵 (T × N)
    2. 跳过最近 1 月，对齐 FF3
    3. 矩阵 OLS: Y = X·B + E，B = (X'X)^{-1}·X'Y
    4. 残差 E 按列求和 = residual momentum

方向：+1
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class ResidualMomFF3(AlphaSignal):
    """Residual Momentum：剔除 FF3（Mkt-RF/SMB/HML）后的残差累加动量。"""

    name = "RESIDUAL_MOM_FF3"
    version = "v1"
    category = "momentum"
    horizon = "month"
    expected_icir = 0.18
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_daily_price", "ff5_daily"]
    ic_window_months = 12

    _LOOKBACK_DAYS = 430  # ~14 月日线（12M + 1M skip + buffer）
    _SKIP_DAYS = 21       # 最近 1 月跳过（经典 12-1 动量）
    _MIN_OBS = 150        # 至少 150 天有效观测

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("ResidualMomFF3: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        ff5 = self.fetch_ff5_factors()
        if ff5.empty:
            logger.warning(f"ResidualMomFF3({date}): 无 FF5 数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])
        ff3 = ff5[["Mkt-RF", "SMB", "HML", "RF"]].copy()

        hist = self.fetch_price_history(
            date, tickers, lookback_days=self._LOOKBACK_DAYS, columns=["adj_close"]
        )
        if hist.empty:
            logger.warning(f"ResidualMomFF3({date}): 无价格")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.Timestamp(date)
        skip_cutoff = date_ts - pd.Timedelta(days=self._SKIP_DAYS)

        # --- 向量化：pivot wide ---
        _FF_COLS = {"Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"}
        hist = hist[~hist["ticker"].isin(_FF_COLS)]

        wide = hist.pivot(index="trade_date", columns="ticker", values="adj_close")
        # 跳过最近 1 月
        wide = wide[wide.index <= skip_cutoff]
        rets = wide.pct_change().iloc[1:]

        # 对齐 FF3
        aligned = rets.join(ff3, how="inner")
        if len(aligned) < self._MIN_OBS:
            logger.warning(f"ResidualMomFF3({date}): 对齐后天数不足 {len(aligned)}")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        ticker_cols = [c for c in aligned.columns if c not in ("Mkt-RF", "SMB", "HML", "RF")]

        # 用前半段估计 beta，后半段算样本外残差累加
        # 这样残差和不会被 intercept 强制为 0
        rf = aligned["RF"].values[:, None]
        Y = aligned[ticker_cols].values - rf  # (T, N) 超额收益

        T, N = Y.shape

        # 分割：前 60% 估计 beta，后 40% 计算残差（约 7 月训练 + 5 月评估）
        split = int(T * 0.6)
        if split < 100:
            logger.warning(f"ResidualMomFF3({date}): 训练集不足 {split}")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        X_full = np.column_stack([
            np.ones(T),
            aligned["Mkt-RF"].values,
            aligned["SMB"].values,
            aligned["HML"].values,
        ])  # (T, 4)

        X_train = X_full[:split]  # (split, 4)
        Y_train = Y[:split]       # (split, N)
        X_test = X_full[split:]   # (T-split, 4)
        Y_test = Y[split:]        # (T-split, N)

        # NaN 处理
        nan_train = ~np.isfinite(Y_train)
        nan_test = ~np.isfinite(Y_test)
        Y_train_clean = np.where(nan_train, 0.0, Y_train)
        Y_test_clean = np.where(nan_test, 0.0, Y_test)

        # 矩阵 OLS on train: B = (X'X)^{-1} X'Y
        try:
            B, *_ = np.linalg.lstsq(X_train, Y_train_clean, rcond=None)
        except np.linalg.LinAlgError:
            logger.warning(f"ResidualMomFF3({date}): lstsq 失败")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 样本外残差
        E_test = Y_test_clean - X_test @ B  # (T-split, N)
        E_test[nan_test] = 0.0

        # 有效观测数（训练+测试都要够）
        n_valid_train = (~nan_train).sum(axis=0)
        n_valid_test = (~nan_test).sum(axis=0)

        # 残差动量 = 测试期残差之和
        cum_resid = E_test.sum(axis=0)  # (N,)
        cum_resid[(n_valid_train < 80) | (n_valid_test < 50)] = np.nan

        out = pd.DataFrame({
            "ticker": ticker_cols,
            "factor_value": cum_resid,
        })
        out = out.dropna(subset=["factor_value"])
        logger.info(f"ResidualMomFF3({date}): {len(out)} 有值")
        return out[["ticker", "factor_value"]].reset_index(drop=True)
