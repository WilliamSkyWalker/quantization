"""
US Stock ML Factor Scorer (LightGBM)

替代/增强线性加权打分，用 LightGBM 学习因子与未来收益的非线性关系。

训练方式：滚动窗口（Rolling Window），每 N 个交易日用过去 12 个月数据重训练。
标签：未来 10 日超额收益（vs S&P 500）。
特征：26 个因子 Z-score（处理后）。

Usage:
    scorer = USMLScorer(db)
    scorer.train(factor_history, price_df, train_end="2024-12-31")
    scores = scorer.predict(current_factor_df)
"""

import logging
import os
from typing import Optional

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# Default hyperparameters
_DEFAULT_PARAMS = {
    "objective": "regression",
    "metric": "mse",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 50,
    "feature_fraction": 0.7,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "lambda_l1": 0.1,
    "lambda_l2": 1.0,
    "verbose": -1,
}
_DEFAULT_N_ESTIMATORS = 200
_DEFAULT_EARLY_STOPPING = 20


def _check_lgb():
    try:
        import lightgbm as lgb
        return lgb
    except ImportError:
        raise ImportError("lightgbm 未安装，请运行: pip install lightgbm")


class USMLScorer:
    """LightGBM-based factor scorer for US stocks."""

    def __init__(self, db, forward_days: int = 10, lookback_months: int = 12):
        self.db = db
        self.forward_days = forward_days
        self.lookback_months = lookback_months
        self.model = None
        self.feature_cols: list[str] = []
        self._feature_importance: Optional[pd.DataFrame] = None

    def train(
        self,
        factor_history: dict[str, pd.DataFrame],
        train_end: str,
        factor_cols: list[str] = None,
        params: dict = None,
    ) -> dict:
        """
        训练 LightGBM 模型。

        Args:
            factor_history: {date_str: DataFrame[ticker, f1, f2, ...]} — 每个调仓日的因子截面
            train_end: 训练截止日期（不包含该日之后的数据）
            factor_cols: 使用的因子列名
            params: LightGBM 超参（可选，默认用 _DEFAULT_PARAMS）

        Returns:
            训练结果摘要 dict（n_samples, n_features, train_mse, feature_importance）
        """
        lgb = _check_lgb()

        train_end_ts = pd.to_datetime(train_end)
        train_start_ts = train_end_ts - pd.DateOffset(months=self.lookback_months)

        # 1. 构建训练数据
        logger.info(f"ML training: {train_start_ts.date()} ~ {train_end_ts.date()}")

        # 加载价格数据用于计算未来收益
        price_df = self._load_prices(train_start_ts, train_end_ts)
        if price_df.empty:
            logger.warning("ML training aborted: no price data")
            return {"error": "no price data"}

        # S&P 500 未来收益
        spx_fwd = self._compute_index_forward_returns(train_start_ts, train_end_ts)

        # 2. 逐日构建 X, y
        all_X = []
        all_y = []

        sorted_dates = sorted(d for d in factor_history.keys()
                              if train_start_ts <= pd.to_datetime(d) <= train_end_ts)

        if not sorted_dates:
            logger.warning("ML training aborted: no factor data in range")
            return {"error": "no factor data"}

        for date_str in sorted_dates:
            df = factor_history[date_str]
            if df.empty:
                continue

            # 确定因子列
            if factor_cols is None:
                factor_cols = [c for c in df.columns if c not in ("ticker", "score", "weight", "side")]
                self.feature_cols = factor_cols

            # 计算未来 N 日超额收益（标签）
            date_ts = pd.to_datetime(date_str)
            fwd_returns = self._compute_forward_returns(price_df, date_ts, self.forward_days)
            if fwd_returns.empty:
                continue

            # S&P 500 同期收益
            spx_ret = spx_fwd.get(date_str, 0.0)

            # 合并特征 + 标签
            merged = df[["ticker"] + factor_cols].merge(
                fwd_returns, on="ticker", how="inner"
            )
            merged["excess_return"] = merged["fwd_return"] - spx_ret

            # 去掉 NaN
            merged = merged.dropna(subset=factor_cols + ["excess_return"])
            if len(merged) < 10:
                continue

            all_X.append(merged[factor_cols].values)
            all_y.append(merged["excess_return"].values)

        if not all_X:
            logger.warning("ML training aborted: insufficient training samples")
            return {"error": "insufficient samples"}

        X = np.vstack(all_X)
        y = np.concatenate(all_y)
        logger.info(f"ML training data: {X.shape[0]} samples, {X.shape[1]} features")

        if X.shape[0] < 500:
            logger.warning(f"ML training: only {X.shape[0]} samples, minimum 500 recommended")

        # 3. 训练 LightGBM
        lgb_params = {**_DEFAULT_PARAMS, **(params or {})}

        # 时间序列分割：后 20% 做验证
        split = int(X.shape[0] * 0.8)
        X_train, X_val = X[:split], X[split:]
        y_train, y_val = y[:split], y[split:]

        train_data = lgb.Dataset(X_train, label=y_train, feature_name=factor_cols)
        val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

        callbacks = [lgb.early_stopping(_DEFAULT_EARLY_STOPPING, verbose=False)]

        self.model = lgb.train(
            lgb_params,
            train_data,
            num_boost_round=_DEFAULT_N_ESTIMATORS,
            valid_sets=[val_data],
            callbacks=callbacks,
        )
        self.feature_cols = factor_cols

        # 4. 特征重要性
        importance = pd.DataFrame({
            "feature": factor_cols,
            "importance": self.model.feature_importance(importance_type="gain"),
        }).sort_values("importance", ascending=False)
        self._feature_importance = importance

        # 验证集 MSE
        val_pred = self.model.predict(X_val)
        val_mse = np.mean((y_val - val_pred) ** 2)

        result = {
            "n_samples": X.shape[0],
            "n_features": X.shape[1],
            "train_dates": len(sorted_dates),
            "val_mse": float(val_mse),
            "val_corr": float(np.corrcoef(y_val, val_pred)[0, 1]) if len(y_val) > 1 else 0,
            "top_features": importance.head(10).to_dict(orient="records"),
        }
        logger.info(
            f"ML training done: {result['n_samples']} samples, "
            f"val_mse={val_mse:.6f}, val_corr={result['val_corr']:.3f}"
        )
        return result

    def predict(self, factor_df: pd.DataFrame) -> pd.Series:
        """
        用训练好的模型预测得分。

        Args:
            factor_df: DataFrame，必须包含 self.feature_cols 列。

        Returns:
            Series（index 同 factor_df），预测得分（Z-score 标准化）。
        """
        if self.model is None:
            raise RuntimeError("Model not trained. Call train() first.")

        missing = set(self.feature_cols) - set(factor_df.columns)
        if missing:
            logger.warning(f"ML predict: missing features {missing}, filling with 0")

        X = factor_df.reindex(columns=self.feature_cols, fill_value=0).values
        # 替换 NaN 为 0（LightGBM 可处理 NaN 但稳妥起见）
        X = np.nan_to_num(X, nan=0.0)

        raw_scores = self.model.predict(X)

        # Z-score 标准化
        mean = np.mean(raw_scores)
        std = np.std(raw_scores)
        if std > 1e-10:
            scores = (raw_scores - mean) / std
        else:
            scores = np.zeros_like(raw_scores)

        return pd.Series(scores, index=factor_df.index)

    @property
    def feature_importance(self) -> Optional[pd.DataFrame]:
        return self._feature_importance

    # ----------------------------------------------------------
    # 内部方法
    # ----------------------------------------------------------

    def _load_prices(self, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        """加载价格数据用于计算未来收益。"""
        # 扩展 end 以便计算 forward returns
        extended_end = (end + pd.Timedelta(days=self.forward_days * 2 + 10)).strftime("%Y-%m-%d")
        start_str = start.strftime("%Y-%m-%d")
        df = self.db.query(
            "SELECT ticker, trade_date, adj_close FROM us_daily_price "
            "WHERE trade_date >= :start AND trade_date <= :end",
            params={"start": start_str, "end": extended_end},
        )
        if not df.empty:
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            df["adj_close"] = pd.to_numeric(df["adj_close"], errors="coerce")
        return df

    def _compute_forward_returns(
        self, price_df: pd.DataFrame, date: pd.Timestamp, days: int
    ) -> pd.DataFrame:
        """计算指定日期起未来 N 交易日的收益率。"""
        # 找到 date 之后的 N 个交易日
        future = price_df[price_df["trade_date"] > date]
        if future.empty:
            return pd.DataFrame(columns=["ticker", "fwd_return"])

        trade_dates = sorted(future["trade_date"].unique())
        if len(trade_dates) < days:
            return pd.DataFrame(columns=["ticker", "fwd_return"])

        target_date = trade_dates[days - 1]

        # 当日价格
        today = price_df[price_df["trade_date"] == date][["ticker", "adj_close"]]
        today = today.rename(columns={"adj_close": "price_now"})

        # 未来价格
        future_px = price_df[price_df["trade_date"] == target_date][["ticker", "adj_close"]]
        future_px = future_px.rename(columns={"adj_close": "price_fwd"})

        merged = today.merge(future_px, on="ticker", how="inner")
        merged["fwd_return"] = (merged["price_fwd"] / merged["price_now"] - 1)
        return merged[["ticker", "fwd_return"]]

    def _compute_index_forward_returns(
        self, start: pd.Timestamp, end: pd.Timestamp
    ) -> dict[str, float]:
        """计算 S&P 500 每日的未来 N 日收益。"""
        extended_end = (end + pd.Timedelta(days=self.forward_days * 2 + 10)).strftime("%Y-%m-%d")
        df = self.db.query(
            "SELECT trade_date, close FROM us_index_daily "
            "WHERE index_code = '^GSPC' AND trade_date >= :start AND trade_date <= :end "
            "ORDER BY trade_date",
            params={"start": start.strftime("%Y-%m-%d"), "end": extended_end},
        )
        if df.empty:
            return {}

        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna().sort_values("trade_date")

        closes = df.set_index("trade_date")["close"]
        result = {}
        for i in range(len(closes) - self.forward_days):
            date = closes.index[i]
            fwd_date = closes.index[i + self.forward_days]
            ret = closes.iloc[i + self.forward_days] / closes.iloc[i] - 1
            result[date.strftime("%Y-%m-%d")] = float(ret)

        return result
