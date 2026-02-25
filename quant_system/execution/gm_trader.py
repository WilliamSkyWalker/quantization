"""
掘金量化模拟盘执行模块

通过掘金量化 Python SDK（gm）实现：
    1. 模拟盘自动下单
    2. TWAP 拆单（大单分批执行）
    3. 持仓同步与对账

使用前提：
    - 安装掘金 SDK: pip install gm
    - 注册掘金量化账号并获取 Token
    - 在 settings.py 中配置 GM_TOKEN 和 GM_STRATEGY_ID

注意事项：
    - 掘金模拟盘仅支持 A 股
    - 下单价格使用市价单，实际成交价由模拟盘撮合
    - TWAP 拆单默认分 5 笔，每笔间隔 1 分钟
    - 所有操作需在交易时段（9:30-15:00）执行
"""

import logging
import os
import time
from typing import Optional

import pandas as pd

from config.settings import LOG_LEVEL

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# 掘金配置（从环境变量或 settings 获取）
GM_TOKEN = os.environ.get("GM_TOKEN", "")
GM_STRATEGY_ID = os.environ.get("GM_STRATEGY_ID", "")


def _check_gm_available() -> bool:
    """检查掘金 SDK 是否可用。"""
    try:
        import gm.api  # noqa: F401
        return True
    except ImportError:
        return False


class GMTrader:
    """
    掘金模拟盘交易执行器。

    用法:
        trader = GMTrader()
        trader.connect()
        trader.sync_position(target_weights_df, total_capital=1000000)
        trader.get_position_report()
    """

    def __init__(
        self,
        token: str = GM_TOKEN,
        strategy_id: str = GM_STRATEGY_ID,
        twap_slices: int = 5,
        twap_interval: int = 60,
    ):
        """
        Args:
            token: 掘金 API Token。
            strategy_id: 策略 ID。
            twap_slices: TWAP 拆单笔数。
            twap_interval: 拆单间隔（秒）。
        """
        self.token = token
        self.strategy_id = strategy_id
        self.twap_slices = twap_slices
        self.twap_interval = twap_interval
        self.connected = False
        self.gm = None

    def connect(self):
        """
        连接掘金模拟盘。

        Raises:
            ImportError: 掘金 SDK 未安装。
            ConnectionError: Token 或 StrategyID 无效。
        """
        if not _check_gm_available():
            raise ImportError(
                "掘金 SDK 未安装，请执行: pip install gm\n"
                "并注册账号获取 Token: https://www.myquant.cn/"
            )

        if not self.token:
            raise ConnectionError("GM_TOKEN 未设置，请配置环境变量或 settings.py")

        import gm.api as gm

        self.gm = gm
        gm.set_token(self.token)
        self.connected = True
        logger.info("掘金模拟盘连接成功")

    def _ensure_connected(self):
        """确保已连接。"""
        if not self.connected:
            raise ConnectionError("请先调用 connect() 连接掘金模拟盘")

    def _to_gm_symbol(self, ts_code: str) -> str:
        """
        将 ts_code 转为掘金格式。

        Args:
            ts_code: 标准代码，如 "000001.SZ"。

        Returns:
            掘金代码，如 "SZSE.000001"。
        """
        code, market = ts_code.split(".")
        if market == "SZ":
            return f"SZSE.{code}"
        elif market == "SH":
            return f"SHSE.{code}"
        return ts_code

    def _from_gm_symbol(self, gm_symbol: str) -> str:
        """
        将掘金代码转为 ts_code 格式。

        Args:
            gm_symbol: 掘金代码，如 "SZSE.000001"。

        Returns:
            标准代码，如 "000001.SZ"。
        """
        parts = gm_symbol.split(".")
        if len(parts) != 2:
            return gm_symbol
        exchange, code = parts
        if exchange == "SZSE":
            return f"{code}.SZ"
        elif exchange == "SHSE":
            return f"{code}.SH"
        return gm_symbol

    # ----------------------------------------------------------
    # 持仓查询
    # ----------------------------------------------------------

    def get_current_positions(self) -> pd.DataFrame:
        """
        获取当前模拟盘持仓。

        Returns:
            持仓 DataFrame，包含 ts_code, volume, market_value, cost 列。
        """
        self._ensure_connected()

        try:
            positions = self.gm.get_position()
        except Exception as e:
            logger.error(f"获取持仓失败: {e}")
            return pd.DataFrame()

        if not positions:
            return pd.DataFrame(columns=["ts_code", "volume", "market_value", "cost"])

        records = []
        for pos in positions:
            records.append({
                "ts_code": self._from_gm_symbol(pos.symbol),
                "volume": pos.volume,
                "market_value": pos.market_value,
                "cost": pos.vwap,
            })

        return pd.DataFrame(records)

    def get_account_info(self) -> dict:
        """
        获取账户信息。

        Returns:
            账户信息字典，包含总资产、可用资金、持仓市值等。
        """
        self._ensure_connected()

        try:
            account = self.gm.get_account()
            return {
                "total_assets": account.nav,
                "available_cash": account.available,
                "market_value": account.market_value,
                "pnl": account.pnl,
            }
        except Exception as e:
            logger.error(f"获取账户信息失败: {e}")
            return {}

    # ----------------------------------------------------------
    # 下单
    # ----------------------------------------------------------

    def order_target_percent(self, ts_code: str, target_percent: float) -> bool:
        """
        按目标比例下单（市价单）。

        Args:
            ts_code: 股票代码。
            target_percent: 目标持仓占总资产的比例（0~1）。

        Returns:
            是否下单成功。
        """
        self._ensure_connected()

        gm_symbol = self._to_gm_symbol(ts_code)

        try:
            self.gm.order_target_percent(
                symbol=gm_symbol,
                percent=target_percent,
                position_side=1,  # 多头
                order_type=2,  # 市价单
            )
            logger.info(f"下单: {ts_code} 目标比例 {target_percent:.2%}")
            return True
        except Exception as e:
            logger.error(f"下单失败 {ts_code}: {e}")
            return False

    def order_twap(
        self, ts_code: str, target_percent: float
    ) -> bool:
        """
        TWAP 拆单执行。

        将目标单量分成 N 笔，每笔间隔固定时间执行。
        适用于大单（权重变化 > 2%）。

        Args:
            ts_code: 股票代码。
            target_percent: 最终目标比例。

        Returns:
            是否全部执行成功。
        """
        self._ensure_connected()

        gm_symbol = self._to_gm_symbol(ts_code)

        # 获取当前持仓比例
        account = self.get_account_info()
        if not account:
            return False

        total_assets = account.get("total_assets", 0)
        if total_assets <= 0:
            return False

        positions = self.get_current_positions()
        current_pct = 0.0
        if not positions.empty:
            pos = positions[positions["ts_code"] == ts_code]
            if not pos.empty:
                current_pct = pos["market_value"].iloc[0] / total_assets

        delta_pct = target_percent - current_pct
        slice_pct = delta_pct / self.twap_slices

        logger.info(
            f"TWAP拆单: {ts_code} {current_pct:.2%} -> {target_percent:.2%}, "
            f"分 {self.twap_slices} 笔"
        )

        success_count = 0
        for i in range(self.twap_slices):
            step_target = current_pct + slice_pct * (i + 1)
            try:
                self.gm.order_target_percent(
                    symbol=gm_symbol,
                    percent=step_target,
                    position_side=1,
                    order_type=2,
                )
                success_count += 1
                logger.debug(f"  第{i + 1}笔: 目标 {step_target:.2%}")
            except Exception as e:
                logger.error(f"  第{i + 1}笔失败: {e}")

            if i < self.twap_slices - 1:
                time.sleep(self.twap_interval)

        return success_count == self.twap_slices

    # ----------------------------------------------------------
    # 持仓同步
    # ----------------------------------------------------------

    def sync_position(
        self,
        target_weights: pd.DataFrame,
        use_twap_threshold: float = 0.02,
    ) -> dict:
        """
        将模拟盘持仓同步到目标权重。

        对于权重变化超过 twap_threshold 的股票使用 TWAP 拆单，
        其余使用普通市价单。

        Args:
            target_weights: 目标权重 DataFrame[ts_code, weight]。
            use_twap_threshold: 使用 TWAP 的权重变化阈值。

        Returns:
            执行结果字典。
        """
        self._ensure_connected()

        account = self.get_account_info()
        if not account:
            return {"status": "error", "message": "获取账户信息失败"}

        total_assets = account["total_assets"]
        current_positions = self.get_current_positions()

        # 计算当前权重
        current_weights = {}
        if not current_positions.empty:
            for _, row in current_positions.iterrows():
                current_weights[row["ts_code"]] = row["market_value"] / total_assets

        # 目标权重
        target = dict(zip(target_weights["ts_code"], target_weights["weight"]))

        # 合并所有需要操作的股票
        all_codes = set(list(current_weights.keys()) + list(target.keys()))

        results = {"success": 0, "failed": 0, "skipped": 0}

        # 先卖后买（释放资金）
        sell_orders = []
        buy_orders = []

        for code in all_codes:
            cur_w = current_weights.get(code, 0)
            tgt_w = target.get(code, 0)
            delta = tgt_w - cur_w

            if abs(delta) < 0.001:
                results["skipped"] += 1
                continue

            if delta < 0:
                sell_orders.append((code, tgt_w, abs(delta)))
            else:
                buy_orders.append((code, tgt_w, delta))

        # 执行卖出
        for code, tgt_w, delta in sell_orders:
            if delta > use_twap_threshold:
                ok = self.order_twap(code, tgt_w)
            else:
                ok = self.order_target_percent(code, tgt_w)
            results["success" if ok else "failed"] += 1

        # 执行买入
        for code, tgt_w, delta in buy_orders:
            if delta > use_twap_threshold:
                ok = self.order_twap(code, tgt_w)
            else:
                ok = self.order_target_percent(code, tgt_w)
            results["success" if ok else "failed"] += 1

        logger.info(
            f"持仓同步完成: 成功 {results['success']}, "
            f"失败 {results['failed']}, 跳过 {results['skipped']}"
        )

        return results

    # ----------------------------------------------------------
    # 对账
    # ----------------------------------------------------------

    def reconcile(self, target_weights: pd.DataFrame) -> pd.DataFrame:
        """
        对账：比较目标权重和实际持仓的差异。

        Args:
            target_weights: 目标权重 DataFrame[ts_code, weight]。

        Returns:
            差异 DataFrame[ts_code, target_weight, actual_weight, diff]。
        """
        self._ensure_connected()

        account = self.get_account_info()
        if not account:
            return pd.DataFrame()

        total_assets = account["total_assets"]
        current = self.get_current_positions()

        target = target_weights.set_index("ts_code")["weight"].to_dict()

        actual = {}
        if not current.empty:
            for _, row in current.iterrows():
                actual[row["ts_code"]] = row["market_value"] / total_assets

        all_codes = set(list(target.keys()) + list(actual.keys()))
        records = []
        for code in sorted(all_codes):
            tgt = target.get(code, 0)
            act = actual.get(code, 0)
            records.append({
                "ts_code": code,
                "target_weight": tgt,
                "actual_weight": act,
                "diff": act - tgt,
            })

        df = pd.DataFrame(records)
        df["diff_abs"] = df["diff"].abs()
        df = df.sort_values("diff_abs", ascending=False)

        # 总偏离度
        total_diff = df["diff_abs"].sum()
        logger.info(f"对账完成: 总偏离度 {total_diff:.2%}")

        return df.drop(columns=["diff_abs"])

    def get_position_report(self) -> str:
        """
        生成持仓报告文本。

        Returns:
            持仓报告字符串。
        """
        self._ensure_connected()

        account = self.get_account_info()
        positions = self.get_current_positions()

        lines = ["=" * 50, "    掘金模拟盘持仓报告", "=" * 50]

        if account:
            lines.append(f"  总资产: {account.get('total_assets', 0):,.2f}")
            lines.append(f"  可用资金: {account.get('available_cash', 0):,.2f}")
            lines.append(f"  持仓市值: {account.get('market_value', 0):,.2f}")
            lines.append(f"  盈亏: {account.get('pnl', 0):,.2f}")

        lines.append("-" * 50)

        if positions.empty:
            lines.append("  无持仓")
        else:
            lines.append(f"  持仓 {len(positions)} 只股票:")
            for _, row in positions.iterrows():
                lines.append(
                    f"    {row['ts_code']:12s} "
                    f"持仓{row['volume']:.0f}股 "
                    f"市值{row['market_value']:,.2f}"
                )

        lines.append("=" * 50)
        return "\n".join(lines)
