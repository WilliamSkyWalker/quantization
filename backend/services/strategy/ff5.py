"""
Fama-French 五因子回归分析

从 Kenneth French Data Library 下载 FF5 日度因子收益，
对策略超额收益做回归，计算 alpha 截距和因子暴露。

Rp - Rf = α + β₁(Mkt-RF) + β₂(SMB) + β₃(HML) + β₄(RMW) + β₅(CMA) + ε

用法：
    from backend.services.strategy.ff5 import FF5Analyzer
    analyzer = FF5Analyzer()
    result = analyzer.analyze(strategy_nav, freq="quarterly")
"""

import io
import logging
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from backend.services.config import LOG_LEVEL, PROJECT_ROOT

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_FF5_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
_CACHE_DIR = PROJECT_ROOT / "backend" / "output" / "ff5_data"


class FF5Analyzer:
    """Fama-French 五因子回归分析器。"""

    def __init__(self):
        self._factors: pd.DataFrame | None = None

    def _load_factors(self) -> pd.DataFrame:
        """加载 FF5 日度因子数据（自动下载缓存）。"""
        if self._factors is not None:
            return self._factors

        cache_path = _CACHE_DIR / "ff5_daily.csv"
        if cache_path.exists():
            import time
            age_days = (time.time() - cache_path.stat().st_mtime) / 86400
            if age_days < 30:
                self._factors = pd.read_csv(cache_path, index_col=0, parse_dates=True)
                logger.info(f"FF5 loaded from cache: {len(self._factors)} days")
                return self._factors

        # Download from Kenneth French library
        logger.info("Downloading FF5 factors from Kenneth French library...")
        import urllib.request
        try:
            req = urllib.request.Request(_FF5_URL, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=30)
            data = resp.read()
        except Exception as e:
            logger.error(f"FF5 download failed: {e}")
            return pd.DataFrame()

        # Extract CSV from zip
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                csv_name = [n for n in zf.namelist() if n.endswith(".CSV") or n.endswith(".csv")][0]
                with zf.open(csv_name) as f:
                    raw = f.read().decode("utf-8")
        except Exception as e:
            logger.error(f"FF5 extract failed: {e}")
            return pd.DataFrame()

        # Parse — skip header rows, find the data start
        lines = raw.strip().split("\n")
        data_start = None
        for i, line in enumerate(lines):
            parts = line.strip().split(",")
            if len(parts) >= 6 and parts[0].strip().isdigit() and len(parts[0].strip()) == 8:
                data_start = i
                break

        if data_start is None:
            logger.error("FF5: cannot find data start in CSV")
            return pd.DataFrame()

        # Read only data rows
        data_rows = []
        for line in lines[data_start:]:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 7 or not parts[0].isdigit():
                break
            try:
                date = pd.to_datetime(parts[0], format="%Y%m%d")
                mkt_rf = float(parts[1]) / 100  # Convert from percentage
                smb = float(parts[2]) / 100
                hml = float(parts[3]) / 100
                rmw = float(parts[4]) / 100
                cma = float(parts[5]) / 100
                rf = float(parts[6]) / 100
                data_rows.append({
                    "date": date, "Mkt-RF": mkt_rf, "SMB": smb,
                    "HML": hml, "RMW": rmw, "CMA": cma, "RF": rf,
                })
            except (ValueError, IndexError):
                continue

        if not data_rows:
            logger.error("FF5: no valid data rows")
            return pd.DataFrame()

        df = pd.DataFrame(data_rows)
        df = df.set_index("date").sort_index()

        # Cache
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache_path)

        self._factors = df
        logger.info(f"FF5 factors loaded: {len(df)} days ({df.index[0].date()} ~ {df.index[-1].date()})")
        return df

    def analyze(
        self,
        strategy_nav: pd.Series,
        freq: str = "quarterly",
    ) -> dict:
        """
        对策略 NAV 做 FF5 回归。

        Args:
            strategy_nav: 策略日净值 Series（DatetimeIndex）。
            freq: "full" 做全期一次回归，"quarterly" 每季度分别回归。

        Returns:
            dict:
                - full: {alpha, t_stat, betas: {Mkt-RF, SMB, HML, RMW, CMA}, r_squared}
                - quarterly: [{period, alpha, t_stat, betas, r_squared}, ...]
        """
        ff5 = self._load_factors()
        if ff5.empty:
            return {"error": "FF5 data not available"}

        # 策略日收益率
        nav = strategy_nav.copy()
        nav.index = pd.to_datetime(nav.index)
        strat_ret = nav.pct_change().dropna()
        strat_ret.name = "strategy"

        # 合并
        merged = pd.DataFrame(strat_ret).join(ff5, how="inner")
        merged = merged.dropna()

        if len(merged) < 30:
            return {"error": f"Insufficient overlapping data: {len(merged)} days"}

        # 超额收益 = 策略收益 - 无风险利率
        merged["excess_ret"] = merged["strategy"] - merged["RF"]

        result = {}

        # 全期回归
        reg = self._run_regression(merged)
        result["full"] = reg
        logger.info(
            f"FF5 full-period: α={reg['alpha_annualized']:.2%} (t={reg['alpha_t_stat']:.2f}), "
            f"R²={reg['r_squared']:.3f}, β_mkt={reg['betas']['Mkt-RF']:.2f}"
        )

        # 季度回归
        if freq == "quarterly":
            merged["quarter"] = merged.index.to_period("Q")
            quarterly = []
            for q, grp in merged.groupby("quarter"):
                if len(grp) < 15:
                    continue
                qreg = self._run_regression(grp)
                qreg["period"] = str(q)
                quarterly.append(qreg)
            result["quarterly"] = quarterly

        return result

    @staticmethod
    def _run_regression(df: pd.DataFrame) -> dict:
        """OLS 回归: excess_ret ~ Mkt-RF + SMB + HML + RMW + CMA"""
        y = df["excess_ret"].values
        factor_names = ["Mkt-RF", "SMB", "HML", "RMW", "CMA"]
        X = df[factor_names].values
        # Add intercept
        X = np.column_stack([np.ones(len(X)), X])

        try:
            # OLS: β = (X'X)⁻¹ X'y
            XtX_inv = np.linalg.pinv(X.T @ X)
            beta = XtX_inv @ X.T @ y
            residuals = y - X @ beta
            n, k = X.shape
            sigma2 = np.sum(residuals ** 2) / (n - k)
            se = np.sqrt(np.diag(sigma2 * XtX_inv))

            alpha = beta[0]
            alpha_se = se[0]
            t_stat = alpha / alpha_se if alpha_se > 1e-10 else 0.0

            # R²
            ss_res = np.sum(residuals ** 2)
            ss_tot = np.sum((y - y.mean()) ** 2)
            r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0

            betas = {name: float(beta[i + 1]) for i, name in enumerate(factor_names)}

            return {
                "alpha_daily": float(alpha),
                "alpha_annualized": float(alpha * 252),
                "alpha_t_stat": float(t_stat),
                "r_squared": float(r_squared),
                "betas": betas,
                "n_obs": n,
            }
        except Exception as e:
            logger.warning(f"FF5 regression failed: {e}")
            return {
                "alpha_daily": 0, "alpha_annualized": 0, "alpha_t_stat": 0,
                "r_squared": 0, "betas": {n: 0 for n in factor_names}, "n_obs": 0,
            }
