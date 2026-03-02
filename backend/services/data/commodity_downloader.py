"""
商品期货数据下载模块

通过 Tushare fut_mapping + fut_daily 接口获取期货主力合约日线数据：
    1. fut_mapping 获取每日主力合约映射（品种→合约代码）
    2. fut_daily 按合约代码拉取日线行情
    3. 拼接后以品种代码（AU/CU/RB…）为维度存入 commodity_price 表

用于商品价格轮动因子（CMDTY_MOM）的数据源。
"""

import logging
from datetime import datetime

import pandas as pd
import tushare as ts

from backend.services.config import (
    TUSHARE_TOKEN,
    COMMODITY_SYMBOLS,
    COMMODITY_EXCHANGE_MAP,
    DATA_START_DATE,
    LOG_LEVEL,
)
from backend.services.data.database import DatabaseManager
from backend.services.data.downloader import TushareRateLimiter, _tushare_call

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class CommodityDownloader:
    """
    商品期货主力合约数据下载器。

    用法:
        db = DatabaseManager()
        dl = CommodityDownloader(db)
        dl.download_commodity_prices("20200101", "20241231")
        dl.update_commodity_prices()  # 增量更新
    """

    def __init__(self, db: DatabaseManager):
        self.db = db
        self.pro = ts.pro_api(TUSHARE_TOKEN)
        self.limiter = TushareRateLimiter()

    def download_commodity_prices(
        self,
        start_date: str = DATA_START_DATE,
        end_date: str | None = None,
        symbols: list[str] | None = None,
    ) -> int:
        """
        全量下载商品期货主力合约日线数据。

        Args:
            start_date: 起始日期，格式 YYYYMMDD。
            end_date: 结束日期，格式 YYYYMMDD，默认今天。
            symbols: 品种列表，默认 COMMODITY_SYMBOLS。

        Returns:
            成功下载的品种数。
        """
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")
        if symbols is None:
            symbols = COMMODITY_SYMBOLS

        success = 0
        for symbol in symbols:
            try:
                df = self._fetch_dominant_daily(symbol, start_date, end_date)
                if df.empty:
                    logger.warning(f"{symbol}: 无数据")
                    continue
                self.db.upsert_commodity_price(df)
                logger.info(f"{symbol}: 下载 {len(df)} 条")
                success += 1
            except Exception as e:
                logger.warning(f"{symbol}: 下载失败 - {e}")

        logger.info(f"商品期货下载完成: {success}/{len(symbols)} 个品种")
        return success

    def update_commodity_prices(self) -> int:
        """
        增量更新商品期货数据，从 DB 最新日期开始。

        Returns:
            成功更新的品种数。
        """
        latest = self.db.get_latest_commodity_date()
        if latest is None:
            logger.info("商品数据为空，执行全量下载")
            return self.download_commodity_prices()

        # 从最新日期开始（包含当天以更新可能缺失的数据）
        start = pd.to_datetime(latest).strftime("%Y%m%d")
        end = datetime.now().strftime("%Y%m%d")
        logger.info(f"商品期货增量更新: {start} ~ {end}")
        return self.download_commodity_prices(start, end)

    def _fetch_dominant_daily(
        self, symbol: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """
        获取单品种主力合约日线数据。

        流程：
            1. fut_mapping 获取主力合约映射
            2. 按合约分段用 fut_daily 拉取价格
            3. 拼接为统一的品种级别日线

        Args:
            symbol: 品种代码，如 "AU"。
            start_date: YYYYMMDD。
            end_date: YYYYMMDD。

        Returns:
            DataFrame[commodity_code, trade_date, ts_code, OHLC, settle, volume, amount, oi]
        """
        exchange = COMMODITY_EXCHANGE_MAP.get(symbol)
        if not exchange:
            logger.warning(f"{symbol}: 无交易所映射")
            return pd.DataFrame()

        # 1. 获取主力合约映射
        mapping = _tushare_call(
            self.pro, "fut_mapping",
            self.limiter,
            ts_code=f"{symbol}.{exchange}",
            start_date=start_date,
            end_date=end_date,
        )

        if mapping.empty:
            return pd.DataFrame()

        # mapping 列: ts_code(品种), trade_date, mapping_ts_code(主力合约代码)
        mapping["trade_date"] = pd.to_datetime(mapping["trade_date"])
        mapping = mapping.sort_values("trade_date")

        # 2. 按合约分段拉取 fut_daily
        all_dfs = []
        for contract_code, grp in mapping.groupby("mapping_ts_code"):
            seg_start = grp["trade_date"].min().strftime("%Y%m%d")
            seg_end = grp["trade_date"].max().strftime("%Y%m%d")

            daily = _tushare_call(
                self.pro, "fut_daily",
                self.limiter,
                ts_code=contract_code,
                start_date=seg_start,
                end_date=seg_end,
            )

            if daily.empty:
                continue

            daily["trade_date"] = pd.to_datetime(daily["trade_date"])
            # 只保留该合约作为主力的日期
            valid_dates = set(grp["trade_date"].tolist())
            daily = daily[daily["trade_date"].isin(valid_dates)]

            if not daily.empty:
                daily["contract_code"] = contract_code
                all_dfs.append(daily)

        if not all_dfs:
            return pd.DataFrame()

        result = pd.concat(all_dfs, ignore_index=True)
        result = result.drop_duplicates(subset=["trade_date"], keep="first")
        result = result.sort_values("trade_date")

        # 3. 标准化输出
        col_map = {
            "ts_code": "ts_code",
            "trade_date": "trade_date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "settle": "settle",
            "vol": "volume",
            "amount": "amount",
            "oi": "oi",
        }
        out = pd.DataFrame()
        for src, dst in col_map.items():
            if src in result.columns:
                out[dst] = result[src]
            else:
                out[dst] = None

        out["commodity_code"] = symbol
        return out
