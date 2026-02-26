"""
财务数据与行业分类下载模块（Tushare Pro 版）

负责从 Tushare Pro 获取以下数据并存入 MySQL：
    1. 季度财务数据（营收、净利润、ROE、毛利率，使用 fina_indicator）
    2. 估值快照（PE/PB/市值，使用 daily_basic）
    3. 行业分类（申万一级 + 二级行业，使用 index_classify + index_member）

Tushare Pro 接口说明：
    - fina_indicator(period): 财务指标，按报告期获取全市场，含公告日期
    - daily_basic(trade_date): 每日指标，PE/PB/市值/换手率
    - index_classify(level, src): 申万行业分类列表
    - index_member(index_code): 行业成分股
"""

import logging
import time
from datetime import datetime
from typing import Optional

import pandas as pd
import tushare as ts
from tqdm import tqdm

from config.settings import (
    DATA_START_DATE,
    TUSHARE_TOKEN,
    LOG_LEVEL,
)
from data.database import DatabaseManager
from data.downloader import TushareRateLimiter, _tushare_call

# ============================================================
# 日志配置
# ============================================================

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


# ============================================================
# 工具函数
# ============================================================

# ============================================================
# 财务数据下载
# ============================================================

class FinancialUpdater:
    """
    财务数据更新器（Tushare Pro 版）

    用法:
        db = DatabaseManager()
        db.init_tables()
        updater = FinancialUpdater(db)
        updater.download_financial_data()
        updater.download_industry_classification()
    """

    def __init__(self, db: DatabaseManager):
        self.db = db
        self.pro = ts.pro_api(TUSHARE_TOKEN)
        self.limiter = TushareRateLimiter()

    # ----------------------------------------------------------
    # 季度财务数据
    # ----------------------------------------------------------

    def download_financial_data(
        self,
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
    ) -> int:
        """
        下载全市场季度财务数据并存入数据库。

        fina_indicator 要求 ts_code 为必填参数（全市场批量需 fina_indicator_vip，5000积分），
        因此按股票遍历：每只股票一次请求获取全部历史季度数据。
        ~5000 只股票 @180/min ≈ 28 分钟。

        Args:
            start_year: 起始年份，默认从 DATA_START_DATE 中提取。
            end_year: 结束年份，默认当前年份。

        Returns:
            成功下载的股票数量。
        """
        if start_year is None:
            start_year = int(DATA_START_DATE[:4])
        if end_year is None:
            end_year = datetime.now().year

        start_date = f"{start_year}0101"
        end_date = f"{end_year}1231"

        # 从 DB 获取股票列表
        df_stocks = self.db.get_stock_list(exclude_st=False)
        if df_stocks.empty:
            logger.error("数据库中无股票列表，请先运行 download_list")
            return 0

        stock_list = df_stocks["ts_code"].tolist()
        logger.info(f"开始下载财务数据: {len(stock_list)} 只股票 ({start_year}~{end_year})")

        success_count = 0
        total_records = 0
        fina_fields = "ts_code,ann_date,end_date,roe_dt,grossprofit_margin,bps"
        income_fields = "ts_code,ann_date,end_date,revenue,n_income"

        for ts_code in tqdm(stock_list, desc="下载财务数据"):
            try:
                df = _tushare_call(
                    self.pro, "fina_indicator", self.limiter,
                    ts_code=ts_code, start_date=start_date, end_date=end_date,
                    fields=fina_fields,
                )

                # 从 income 接口补充 revenue 和 net_profit
                df_income = self._fetch_income(ts_code, start_date, end_date, income_fields)
                df = self._merge_income(df, df_income)

                if df.empty:
                    continue

                df_write = self._process_financial_df(df)
                if df_write is not None and not df_write.empty:
                    self.db.upsert_financial_data(df_write)
                    success_count += 1
                    total_records += len(df_write)

            except Exception as e:
                logger.debug(f"{ts_code} 财务数据下载失败: {e}")

        logger.info(
            f"财务数据下载完成: {success_count}/{len(stock_list)} 只股票, "
            f"共 {total_records} 条记录"
        )
        return success_count

    def _process_financial_df(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        """处理 fina_indicator 返回的 DataFrame，统一列名并清洗。"""
        if df.empty:
            return None

        # 列名映射
        col_map = {
            "roe_dt": "roe_ttm",
            "grossprofit_margin": "gross_margin",
            "n_income": "net_profit",
        }
        df = df.rename(columns=col_map)

        # 日期处理（NaT → None）
        for col in ["end_date", "ann_date"]:
            df[col] = pd.to_datetime(df[col], errors="coerce")
            df[col] = df[col].apply(lambda x: x.date() if pd.notna(x) else None)

        # 缺失公告日期的，用法定披露截止日估算
        for idx, row in df.iterrows():
            if row["ann_date"] is None and row["end_date"] is not None:
                end_str = row["end_date"].strftime("%Y%m%d")
                df.at[idx, "ann_date"] = self._estimate_ann_date(end_str)

        # 数值列转换
        for col in ["roe_ttm", "gross_margin", "revenue", "net_profit", "bps"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # PE/PB/市值暂不从此接口获取
        df["pe_ttm"] = None
        df["pb"] = None
        df["total_mv"] = None
        df["circ_mv"] = None

        # 同一 ts_code + end_date 去重（保留公告日最新的）
        df = df.sort_values("ann_date", ascending=False).drop_duplicates(
            subset=["ts_code", "end_date"], keep="first"
        )

        # 组装写入数据
        write_cols = [
            "ts_code", "ann_date", "end_date",
            "pe_ttm", "pb", "roe_ttm", "gross_margin",
            "revenue", "net_profit", "bps", "total_mv", "circ_mv",
        ]
        df_write = df[[c for c in write_cols if c in df.columns]].copy()

        # 去除全为空的记录
        value_cols = ["roe_ttm", "gross_margin", "revenue", "net_profit"]
        existing_value_cols = [c for c in value_cols if c in df_write.columns]
        if existing_value_cols:
            df_write = df_write.dropna(subset=existing_value_cols, how="all")

        return df_write if not df_write.empty else None

    def _fetch_income(
        self,
        ts_code: str,
        start_date: str,
        end_date: Optional[str],
        fields: str,
    ) -> pd.DataFrame:
        """
        从 income（利润表）接口获取 revenue 和 n_income。

        fina_indicator 在低积分权限下不返回 revenue/n_income，
        需要从 income 接口单独获取。
        """
        try:
            kwargs = dict(ts_code=ts_code, start_date=start_date, fields=fields)
            if end_date:
                kwargs["end_date"] = end_date
            df = _tushare_call(self.pro, "income", self.limiter, **kwargs)
            return df
        except Exception as e:
            logger.debug(f"{ts_code} income 接口调用失败: {e}")
            return pd.DataFrame()

    @staticmethod
    def _merge_income(df_fina: pd.DataFrame, df_income: pd.DataFrame) -> pd.DataFrame:
        """
        将 income 接口的 revenue/n_income 合并到 fina_indicator 的结果中。

        以 (ts_code, end_date) 为键做 left join，补充缺失的 revenue 和 n_income 列。
        """
        if df_fina.empty:
            return df_fina

        if df_income.empty or df_income is None:
            # 无 income 数据，确保列存在
            if "revenue" not in df_fina.columns:
                df_fina["revenue"] = None
            if "n_income" not in df_fina.columns:
                df_fina["n_income"] = None
            return df_fina

        # income 接口可能有重复行（同一 end_date 多次公告），去重保留最新
        df_income = df_income.sort_values("ann_date", ascending=False).drop_duplicates(
            subset=["ts_code", "end_date"], keep="first"
        )

        # 只取需要的列
        income_cols = ["ts_code", "end_date"]
        if "revenue" in df_income.columns:
            income_cols.append("revenue")
        if "n_income" in df_income.columns:
            income_cols.append("n_income")
        df_income = df_income[income_cols]

        # 合并
        df = df_fina.merge(df_income, on=["ts_code", "end_date"], how="left", suffixes=("", "_inc"))

        # 如果 fina_indicator 已有 revenue/n_income 列但为空，用 income 的填充
        for col in ["revenue", "n_income"]:
            col_inc = f"{col}_inc"
            if col_inc in df.columns:
                if col in df.columns:
                    df[col] = df[col].fillna(df[col_inc])
                else:
                    df[col] = df[col_inc]
                df = df.drop(columns=[col_inc])
            elif col not in df.columns:
                df[col] = None

        return df

    @staticmethod
    def _estimate_ann_date(quarter_date: str):
        """
        根据报告期估算法定披露截止日（保守估计）。

        A股财报披露截止日规则：
            - Q1（一季报）：4月30日
            - Q2（半年报）：8月31日
            - Q3（三季报）：10月31日
            - Q4（年报）：次年4月30日
        """
        year = int(quarter_date[:4])
        month = int(quarter_date[4:6])

        if month == 3:
            return pd.to_datetime(f"{year}0430").date()
        elif month == 6:
            return pd.to_datetime(f"{year}0831").date()
        elif month == 9:
            return pd.to_datetime(f"{year}1031").date()
        elif month == 12:
            return pd.to_datetime(f"{year + 1}0430").date()
        return pd.to_datetime(quarter_date).date()

    # ----------------------------------------------------------
    # 财务数据增量更新
    # ----------------------------------------------------------

    def update_financial_data(self) -> int:
        """
        增量更新财务数据 — 只补齐最近 2 个季度的缺失股票。

        逻辑：
            1. 算出当前应存在的最近 2 个报告期（如 20240930, 20241231）
            2. 查 DB 中这 2 个季度已有数据的 ts_code 集合
            3. 只对缺失的股票调用 fina_indicator

        典型场景（季报披露季）：增量约 500~2000 只，<15 分钟。
        非披露季：大部分已有，<1 分钟。

        Returns:
            成功更新的股票数量。
        """
        # 算最近 2 个报告期
        recent_quarters = self._get_recent_quarters(n=2)
        if not recent_quarters:
            logger.warning("无法确定最近报告期")
            return 0

        logger.info(f"增量更新财务数据，目标报告期: {recent_quarters}")

        # 全部股票
        df_stocks = self.db.get_stock_list(exclude_st=False)
        if df_stocks.empty:
            logger.error("数据库中无股票列表")
            return 0
        all_codes = set(df_stocks["ts_code"].tolist())

        # 查 DB 中已有数据的股票（这 2 个季度的并集）
        q_list = "','".join(recent_quarters)
        existing = self.db.query(
            f"SELECT DISTINCT ts_code FROM financial_data "
            f"WHERE end_date IN ('{q_list}')"
        )
        existing_codes = set(existing["ts_code"].tolist()) if not existing.empty else set()

        missing_codes = sorted(all_codes - existing_codes)
        logger.info(
            f"全市场 {len(all_codes)} 只，已有 {len(existing_codes)} 只，"
            f"待更新 {len(missing_codes)} 只"
        )

        if not missing_codes:
            logger.info("财务数据已是最新")
            return 0

        # 对缺失股票逐只拉取最近 2 个季度
        start_date = recent_quarters[-1]  # 较早的那个季度
        fina_fields = "ts_code,ann_date,end_date,roe_dt,grossprofit_margin,bps"
        income_fields = "ts_code,ann_date,end_date,revenue,n_income"
        success_count = 0

        for ts_code in tqdm(missing_codes, desc="增量更新财务"):
            try:
                df = _tushare_call(
                    self.pro, "fina_indicator", self.limiter,
                    ts_code=ts_code, start_date=start_date,
                    fields=fina_fields,
                )

                df_income = self._fetch_income(ts_code, start_date, None, income_fields)
                df = self._merge_income(df, df_income)

                if df.empty:
                    continue

                df_write = self._process_financial_df(df)
                if df_write is not None and not df_write.empty:
                    self.db.upsert_financial_data(df_write)
                    success_count += 1
            except Exception as e:
                logger.debug(f"{ts_code} 增量更新失败: {e}")

        logger.info(f"财务数据增量更新完成: {success_count}/{len(missing_codes)} 只")
        return success_count

    # ----------------------------------------------------------
    # 回填历史 revenue / net_profit
    # ----------------------------------------------------------

    def backfill_income(self) -> int:
        """
        回填历史财务数据中缺失的 revenue / net_profit。

        只针对 financial_data 表中 revenue 或 net_profit 为 NULL 的记录，
        从 income 接口补充数据，不重新下载 fina_indicator。

        Returns:
            成功更新的股票数量。
        """
        # 找出需要回填的股票
        df_missing = self.db.query(
            "SELECT DISTINCT ts_code FROM financial_data "
            "WHERE revenue IS NULL OR net_profit IS NULL"
        )

        if df_missing.empty:
            logger.info("所有财务记录的 revenue/net_profit 均已有值，无需回填")
            return 0

        missing_codes = df_missing["ts_code"].tolist()
        logger.info(f"需回填 income 数据的股票: {len(missing_codes)} 只")

        income_fields = "ts_code,ann_date,end_date,revenue,n_income"
        start_date = DATA_START_DATE
        success_count = 0
        total_updated = 0

        for ts_code in tqdm(missing_codes, desc="回填 income 数据"):
            try:
                df_income = self._fetch_income(ts_code, start_date, None, income_fields)
                if df_income.empty:
                    continue

                # 去重：同一 end_date 保留最新公告
                df_income = df_income.sort_values("ann_date", ascending=False).drop_duplicates(
                    subset=["ts_code", "end_date"], keep="first"
                )

                # 逐条更新
                updated = 0
                with self.db.engine.begin() as conn:
                    for _, row in df_income.iterrows():
                        end_date = row.get("end_date")
                        revenue = row.get("revenue")
                        n_income = row.get("n_income")

                        if pd.isna(end_date):
                            continue

                        # 只更新 NULL 字段
                        set_parts = []
                        params = {"ts_code": ts_code, "end_date": str(end_date)}

                        if pd.notna(revenue):
                            set_parts.append("revenue = CASE WHEN revenue IS NULL THEN :revenue ELSE revenue END")
                            params["revenue"] = float(revenue)
                        if pd.notna(n_income):
                            set_parts.append("net_profit = CASE WHEN net_profit IS NULL THEN :net_profit ELSE net_profit END")
                            params["net_profit"] = float(n_income)

                        if set_parts:
                            from sqlalchemy import text as sa_text
                            sql = sa_text(
                                f"UPDATE financial_data SET {', '.join(set_parts)} "
                                f"WHERE ts_code = :ts_code AND end_date = :end_date"
                            )
                            result = conn.execute(sql, params)
                            updated += result.rowcount

                if updated > 0:
                    success_count += 1
                    total_updated += updated

            except Exception as e:
                logger.debug(f"{ts_code} income 回填失败: {e}")

        logger.info(
            f"income 回填完成: {success_count}/{len(missing_codes)} 只股票, "
            f"共更新 {total_updated} 条记录"
        )
        return success_count

    @staticmethod
    def _get_recent_quarters(n: int = 2) -> list[str]:
        """返回当前日期之前最近 n 个报告期（YYYYMMDD），按倒序排列。"""
        today = datetime.now()
        year, month = today.year, today.month
        # 按倒序生成所有季度端点
        quarters = []
        y = year
        while len(quarters) < n:
            for q_end in ["1231", "0930", "0630", "0331"]:
                qdate = f"{y}{q_end}"
                if qdate <= today.strftime("%Y%m%d"):
                    quarters.append(qdate)
                    if len(quarters) >= n:
                        break
            y -= 1
        return quarters

    # ----------------------------------------------------------
    # 估值数据快照（PE/PB/市值）
    # ----------------------------------------------------------

    def download_valuation_snapshot(self) -> int:
        """
        下载当前全市场估值快照（PE_TTM、PB、总市值、流通市值）。

        使用 Tushare 的 daily_basic 接口获取最新交易日的估值数据，
        并更新到最近一期财务数据记录中。

        Returns:
            更新的记录数。
        """
        logger.info("下载估值快照...")

        # 获取最近有数据的交易日（今天可能还没收盘，需回退）
        today = datetime.now().strftime("%Y%m%d")
        df_cal = _tushare_call(self.pro, "trade_cal", self.limiter,
                               exchange="SSE", end_date=today, is_open="1",
                               fields="cal_date", limit="5")
        if df_cal.empty:
            logger.error("无法获取最近交易日")
            return 0

        # 从最近交易日开始尝试，找到有数据的那天
        df = pd.DataFrame()
        latest_trade_date = None
        for cal_date in df_cal["cal_date"].tolist():
            df = _tushare_call(self.pro, "daily_basic", self.limiter,
                               trade_date=cal_date,
                               fields="ts_code,pe_ttm,pb,total_mv,circ_mv,total_share,float_share")
            if not df.empty:
                latest_trade_date = cal_date
                break

        if df.empty:
            logger.error("daily_basic 返回为空（最近5个交易日均无数据）")
            return 0

        # 筛选沪深两市
        df = df[df["ts_code"].str.match(r"^(00|30|60|68)")].copy()

        # 数值转换
        for col in ["pe_ttm", "pb", "total_mv", "circ_mv", "total_share", "float_share"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # 将 total_share / float_share 写入 stock_basic 表
        share_cols = ["ts_code", "total_share", "float_share"]
        df_share = df[[c for c in share_cols if c in df.columns]].dropna(subset=["total_share"]).copy()
        if not df_share.empty:
            self.db.upsert_stock_basic(df_share)
            logger.info(f"stock_basic: 更新 {len(df_share)} 条股本数据")

        # 更新到数据库中最近一期财务数据
        latest_quarter = self._get_latest_quarter()

        if latest_quarter is None:
            logger.warning("数据库无财务数据，请先下载财务数据")
            return 0

        # 组装批量更新数据
        updates = []
        for _, row in df.iterrows():
            ts_code = row.get("ts_code")
            if not ts_code:
                continue

            record = {"ts_code": ts_code}
            for col in ["pe_ttm", "pb", "total_mv", "circ_mv"]:
                val = row.get(col)
                if pd.notna(val):
                    record[col] = float(val)

            if len(record) > 1:
                updates.append(record)

        # 分批执行
        update_count = 0
        BATCH = 500
        for i in tqdm(range(0, len(updates), BATCH), desc="批量更新估值"):
            batch = updates[i:i + BATCH]
            try:
                self.db.batch_update_financial(batch, latest_quarter)
                update_count += len(batch)
            except Exception as e:
                logger.warning(f"批量更新失败: {e}")

        logger.info(f"估值快照更新完成: {update_count} 条 (交易日: {latest_trade_date})")
        return update_count

    def _get_latest_quarter(self) -> Optional[str]:
        """获取数据库中最新的报告期。"""
        result = self.db.query("SELECT MAX(end_date) as max_date FROM financial_data")
        val = result["max_date"].iloc[0]
        if pd.isna(val):
            return None
        return str(val)

    # ----------------------------------------------------------
    # 行业分类（申万一级 + 二级）
    # ----------------------------------------------------------

    def download_industry_classification(self) -> int:
        """
        下载全市场行业分类并存入数据库。

        使用申万行业标准分类（SW2021）：
            1. index_classify(level='L1', src='SW2021') 获取一级行业列表
            2. index_member(index_code=c) 逐个获取一级成分股
            3. index_classify(level='L2', src='SW2021') 获取二级行业列表
            4. index_member(index_code=c) 逐个获取二级成分股
            5. 合并 L1 + L2 数据写入数据库

        Returns:
            写入的记录数。
        """
        logger.info("开始下载行业分类（申万一级 + 二级）...")

        # ========== L1 一级行业 ==========
        df_index_l1 = _tushare_call(self.pro, "index_classify", self.limiter,
                                     level="L1", src="SW2021")

        if df_index_l1.empty:
            logger.error("申万一级行业分类列表为空")
            return 0

        logger.info(f"共 {len(df_index_l1)} 个申万一级行业")

        all_records = []
        failed_industries = []

        for _, row in tqdm(df_index_l1.iterrows(), total=len(df_index_l1), desc="下载L1行业成分股"):
            index_code = row.get("index_code")
            industry_name = row.get("industry_name", "")

            if not index_code:
                continue

            try:
                df_members = _tushare_call(self.pro, "index_member", self.limiter,
                                           index_code=index_code,
                                           fields="index_code,con_code,in_date,out_date")

                if not df_members.empty:
                    # 只保留当前在列的成分股（out_date 为空）
                    current = df_members[df_members["out_date"].isna() | (df_members["out_date"] == "")]

                    for _, m_row in current.iterrows():
                        ts_code = m_row.get("con_code", "")
                        if ts_code and ts_code[:2] in ("00", "30", "60", "68"):
                            all_records.append({
                                "ts_code": ts_code,
                                "industry_code": index_code,
                                "industry_name": industry_name,
                            })

            except Exception as e:
                failed_industries.append(industry_name)
                logger.debug(f"L1 行业 {industry_name}({index_code}) 获取失败: {e}")

        if failed_industries:
            logger.warning(
                f"L1: {len(failed_industries)} 个行业获取失败: {failed_industries[:5]}..."
            )

        if not all_records:
            logger.error("未获取到任何 L1 行业分类数据")
            return 0

        df_industry = pd.DataFrame(all_records)
        # 一只股票只保留一个 L1 行业
        df_industry = df_industry.drop_duplicates(subset=["ts_code"], keep="first")

        # ========== L2 二级行业 ==========
        logger.info("开始下载申万二级行业...")
        df_index_l2 = _tushare_call(self.pro, "index_classify", self.limiter,
                                     level="L2", src="SW2021")

        if not df_index_l2.empty:
            logger.info(f"共 {len(df_index_l2)} 个申万二级行业")

            l2_map = {}  # ts_code -> (l2_code, l2_name)
            failed_l2 = []

            for _, row in tqdm(df_index_l2.iterrows(), total=len(df_index_l2), desc="下载L2行业成分股"):
                index_code = row.get("index_code")
                industry_name = row.get("industry_name", "")

                if not index_code:
                    continue

                try:
                    df_members = _tushare_call(self.pro, "index_member", self.limiter,
                                               index_code=index_code,
                                               fields="index_code,con_code,in_date,out_date")

                    if not df_members.empty:
                        current = df_members[df_members["out_date"].isna() | (df_members["out_date"] == "")]

                        for _, m_row in current.iterrows():
                            ts_code = m_row.get("con_code", "")
                            if ts_code and ts_code[:2] in ("00", "30", "60", "68"):
                                # 一只股票只保留第一个匹配的 L2 行业
                                if ts_code not in l2_map:
                                    l2_map[ts_code] = (index_code, industry_name)

                except Exception as e:
                    failed_l2.append(industry_name)
                    logger.debug(f"L2 行业 {industry_name}({index_code}) 获取失败: {e}")

            if failed_l2:
                logger.warning(
                    f"L2: {len(failed_l2)} 个行业获取失败: {failed_l2[:5]}..."
                )

            # 合并 L2 到 L1 DataFrame
            df_industry["l2_industry_code"] = df_industry["ts_code"].map(
                lambda x: l2_map.get(x, (None, None))[0]
            )
            df_industry["l2_industry_name"] = df_industry["ts_code"].map(
                lambda x: l2_map.get(x, (None, None))[1]
            )
            logger.info(f"L2 行业匹配: {df_industry['l2_industry_name'].notna().sum()}/{len(df_industry)} 只股票")
        else:
            logger.warning("申万二级行业列表为空，跳过 L2")
            df_industry["l2_industry_code"] = None
            df_industry["l2_industry_name"] = None

        # 写入数据库
        self.db.upsert_industry_class(df_industry)
        logger.info(f"行业分类下载完成: {len(df_industry)} 只股票")

        # 打印行业分布
        dist = df_industry["industry_name"].value_counts()
        logger.info(f"L1 行业分布（前10）:\n{dist.head(10).to_string()}")

        l2_dist = df_industry["l2_industry_name"].value_counts()
        if not l2_dist.empty:
            logger.info(f"L2 行业分布（前10）:\n{l2_dist.head(10).to_string()}")

        return len(df_industry)


# ============================================================
# 命令行入口
# ============================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s",
    )

    db = DatabaseManager()
    db.init_tables()
    updater = FinancialUpdater(db)

    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    if mode == "financial":
        print("=== 下载财务数据 ===")
        count = updater.download_financial_data()
        print(f"完成，成功 {count} 个季度")

    elif mode == "valuation":
        print("=== 下载估值快照 ===")
        count = updater.download_valuation_snapshot()
        print(f"完成，更新 {count} 条")

    elif mode == "industry":
        print("=== 下载行业分类 ===")
        count = updater.download_industry_classification()
        print(f"完成，{count} 只股票")

    elif mode == "all":
        print("=== 全量下载财务数据和行业分类 ===")
        print("步骤 1/3: 下载财务数据...")
        fin_count = updater.download_financial_data()
        print(f"财务数据完成，{fin_count} 个季度")

        print("步骤 2/3: 下载估值快照...")
        val_count = updater.download_valuation_snapshot()
        print(f"估值快照完成，{val_count} 条")

        print("步骤 3/3: 下载行业分类...")
        ind_count = updater.download_industry_classification()
        print(f"行业分类完成，{ind_count} 只")

    else:
        print("用法: python -m data.updater [financial|valuation|industry|all]")
        sys.exit(1)
