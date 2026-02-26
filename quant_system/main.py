"""
A股量化投资系统 - 主入口

用法:
    # ====== 初始化 ======
    python3 main.py init                 # 初始化数据库表结构

    # ====== 数据下载（Phase 1）======
    python3 main.py download_list        # 下载股票列表
    python3 main.py download_daily       # 下载全量日线行情
    python3 main.py download_all         # 一键全量下载（股票列表 + 日线）
    python3 main.py download_financial   # 下载季度财务数据
    python3 main.py download_valuation   # 下载当前估值快照（PE/PB/市值）
    python3 main.py download_industry    # 下载行业分类
    python3 main.py download_index        # 下载沪深300指数日线
    python3 main.py download_extra       # 一键下载财务 + 估值 + 行业

    # ====== 增量更新 ======
    python3 main.py update               # 增量更新日线行情
    python3 main.py update_financial     # 增量更新财务数据（最近2个季度）
    python3 main.py update_all           # 一键增量：股票列表+日线+财务+估值+行业

    # ====== 数据清洗 ======
    python3 main.py universe [YYYY-MM-DD]  # 查看某日可交易股票池

    # ====== 策略回测（Phase 2~4）======
    python3 main.py backtest [start] [end]   # 运行多因子策略回测（T+1执行）
    python3 main.py select [YYYY-MM-DD]      # 单日选股（查看当期信号）

    # ====== 模拟盘（Phase 5）======
    python3 main.py trade                    # T+1执行：用T日信号在T+1日开盘价成交
    python3 main.py position                 # 查看模拟盘持仓
    python3 main.py paper_replay [start] [end]  # 回放历史交易
    python3 main.py paper_nav                # 查看净值历史
    python3 main.py paper_transactions       # 查看交易记录
    python3 main.py paper_reset              # 重置模拟账户

    # ====== 报告（Phase 6）======
    python3 main.py report [start] [end]     # 生成策略报告

    # ====== 行业因子配置 ======
    python3 main.py seed_industry_config     # 初始化行业因子权重种子数据
    python3 main.py show_industry_config     # 查看当前行业因子权重配置

    # ====== 舆情抓取 ======
    python3 main.py download_sentiment              # 全量抓取 11 个政府网站
    python3 main.py download_sentiment --source=csrc # 单源抓取
    python3 main.py download_sentiment --tier=3      # 按层级抓取
    python3 main.py update_sentiment                 # 增量更新（同 download，URL 去重）
    python3 main.py sentiment_status                 # 各来源文章数和最新日期

    # ====== 状态查看 ======
    python3 main.py status               # 查看数据库状态
"""

import logging
import sys

from config.settings import (
    LOG_LEVEL, LOG_FORMAT, LOG_DATE_FORMAT,
    TRADER_TYPE, PAPER_INITIAL_CAPITAL,
    INDUSTRY_INDEX_MAP,
)
from data.database import DatabaseManager


def setup_logging():
    """配置全局日志。"""
    logging.basicConfig(
        level=LOG_LEVEL,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
    )


def _create_trader(db: DatabaseManager):
    """根据 TRADER_TYPE 配置创建交易执行器。"""
    if TRADER_TYPE == "paper":
        from execution.paper_trader import PaperTrader
        trader = PaperTrader(db)
        trader.connect()
        return trader
    elif TRADER_TYPE == "qmt":
        raise NotImplementedError("QMT 交易执行器尚未实现，敬请期待")
    elif TRADER_TYPE == "ptrade":
        raise NotImplementedError("Ptrade 交易执行器尚未实现，敬请期待")
    else:
        raise ValueError(f"未知 TRADER_TYPE: {TRADER_TYPE}，可选: paper / qmt / ptrade")


def main():
    setup_logging()
    logger = logging.getLogger("main")

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    command = sys.argv[1]
    db = DatabaseManager()

    # ==============================================================
    # 初始化
    # ==============================================================
    if command == "init":
        print("=== 初始化数据库表结构 ===")
        db.init_tables()
        print("完成！表结构已创建。")

    # ==============================================================
    # 数据下载
    # ==============================================================
    elif command == "download_list":
        from data.downloader import TushareDownloader
        db.init_tables()
        downloader = TushareDownloader(db)
        print("=== 下载股票列表 ===")
        df = downloader.download_stock_list()
        print(f"完成，共 {len(df)} 只股票")

    elif command == "download_daily":
        from data.downloader import TushareDownloader
        db.init_tables()
        downloader = TushareDownloader(db)
        print("=== 下载日线行情 ===")
        count = downloader.download_daily_prices()
        print(f"完成，成功下载 {count} 只股票的日线数据")

    elif command == "download_all":
        from data.downloader import TushareDownloader
        db.init_tables()
        downloader = TushareDownloader(db)
        print("=== 全量下载 ===")
        print("步骤 1/3: 下载股票列表...")
        df = downloader.download_stock_list()
        print(f"股票列表完成，共 {len(df)} 只")
        print("步骤 2/3: 下载日线行情（预计耗时较长）...")
        count = downloader.download_daily_prices()
        print(f"日线行情完成，成功 {count} 只")
        print("步骤 3/3: 下载沪深300指数日线...")
        idx_count = downloader.download_index_daily("000300.SH")
        print(f"沪深300指数完成，{idx_count} 条")

    elif command == "download_financial":
        from data.updater import FinancialUpdater
        db.init_tables()
        updater = FinancialUpdater(db)
        print("=== 下载季度财务数据 ===")
        count = updater.download_financial_data()
        print(f"完成，成功 {count} 个季度")

    elif command == "download_valuation":
        from data.updater import FinancialUpdater
        db.init_tables()
        updater = FinancialUpdater(db)
        print("=== 下载估值快照 ===")
        count = updater.download_valuation_snapshot()
        print(f"完成，更新 {count} 条")

    elif command == "download_industry":
        from data.updater import FinancialUpdater
        db.init_tables()
        updater = FinancialUpdater(db)
        print("=== 下载行业分类 ===")
        count = updater.download_industry_classification()
        print(f"完成，{count} 只股票")

    elif command == "download_index":
        from data.downloader import TushareDownloader
        db.init_tables()
        downloader = TushareDownloader(db)
        print("=== 下载沪深300指数日线 ===")
        count = downloader.download_index_daily("000300.SH")
        print(f"完成，{count} 条记录")

        # 下载行业指数
        if INDUSTRY_INDEX_MAP:
            print("=== 下载行业指数日线 ===")
            for ind_name, ind_code in INDUSTRY_INDEX_MAP.items():
                print(f"  下载 {ind_name}({ind_code})...")
                ind_count = downloader.download_index_daily(ind_code)
                print(f"  完成，{ind_count} 条记录")

    elif command == "backfill_income":
        from data.updater import FinancialUpdater
        db.init_tables()
        updater = FinancialUpdater(db)
        print("=== 回填 revenue/net_profit（从 income 接口补充）===")
        count = updater.backfill_income()
        print(f"完成，成功回填 {count} 只股票")

    elif command == "download_extra":
        from data.updater import FinancialUpdater
        db.init_tables()
        updater = FinancialUpdater(db)
        print("=== 下载财务数据 + 估值 + 行业 ===")

        print("步骤 1/3: 下载季度财务数据...")
        fin = updater.download_financial_data()
        print(f"  财务数据完成，{fin} 个季度")

        print("步骤 2/3: 下载估值快照...")
        val = updater.download_valuation_snapshot()
        print(f"  估值快照完成，{val} 条")

        print("步骤 3/3: 下载行业分类...")
        ind = updater.download_industry_classification()
        print(f"  行业分类完成，{ind} 只")

    # ==============================================================
    # 增量更新
    # ==============================================================
    elif command == "update":
        from data.downloader import TushareDownloader
        db.init_tables()
        downloader = TushareDownloader(db)
        print("=== 增量更新日线行情 ===")
        count = downloader.update_daily_prices()
        print(f"完成，成功更新 {count} 只")

    elif command == "update_financial":
        from data.updater import FinancialUpdater
        db.init_tables()
        updater = FinancialUpdater(db)
        print("=== 增量更新财务数据 ===")
        count = updater.update_financial_data()
        print(f"完成，更新 {count} 只")

    elif command == "update_all":
        from data.downloader import TushareDownloader
        from data.updater import FinancialUpdater
        db.init_tables()
        downloader = TushareDownloader(db)
        updater = FinancialUpdater(db)

        print("=== 全量增量更新 ===")

        print("步骤 1/6: 刷新股票列表（更新ST状态）...")
        df = downloader.download_stock_list()
        print(f"  股票列表刷新完成，{len(df)} 只")

        print("步骤 2/6: 增量更新日线行情...")
        daily_count = downloader.update_daily_prices()
        print(f"  日线更新完成，{daily_count} 个交易日")

        print("步骤 3/6: 增量更新沪深300指数...")
        idx_count = downloader.update_index_daily("000300.SH")
        print(f"  沪深300指数完成，{idx_count} 条")

        # 增量更新行业指数
        if INDUSTRY_INDEX_MAP:
            print("  增量更新行业指数...")
            for ind_name, ind_code in INDUSTRY_INDEX_MAP.items():
                ind_count = downloader.update_index_daily(ind_code)
                print(f"  {ind_name}({ind_code}) 完成，{ind_count} 条")

        print("步骤 4/6: 增量更新财务数据...")
        fin_count = updater.update_financial_data()
        print(f"  财务数据完成，{fin_count} 只")

        print("步骤 5/6: 刷新估值快照...")
        val_count = updater.download_valuation_snapshot()
        print(f"  估值快照完成，{val_count} 条")

        print("步骤 6/6: 刷新行业分类...")
        ind_count = updater.download_industry_classification()
        print(f"  行业分类完成，{ind_count} 只")

    # ==============================================================
    # 数据清洗 / 股票池
    # ==============================================================
    elif command == "universe":
        from data.cleaner import get_clean_universe
        db.init_tables()
        target = sys.argv[2] if len(sys.argv) > 2 else None
        if target is None:
            target = db.get_latest_trade_date()
            if target is None:
                print("数据库中无行情数据，请先下载")
                sys.exit(1)

        print(f"=== 构建 {target} 可交易股票池 ===")
        df = get_clean_universe(db, target)
        if df.empty:
            print("股票池为空（可能非交易日）")
        else:
            print(f"\n可交易股票: {len(df)} 只")
            limit_up = df["is_limit_up"].sum() if "is_limit_up" in df.columns else 0
            limit_down = df["is_limit_down"].sum() if "is_limit_down" in df.columns else 0
            print(f"涨停: {limit_up} 只（不可买入）  跌停: {limit_down} 只（不可卖出）")
            if "industry_name" in df.columns and df["industry_name"].notna().any():
                print(f"\n行业分布:")
                print(df["industry_name"].value_counts().head(10).to_string())
            print(f"\n前10只:")
            print(df.head(10).to_string(index=False))

    # ==============================================================
    # 策略回测
    # ==============================================================
    elif command == "backtest":
        from strategy.multi_factor import MultiFactorStrategy
        from strategy.backtest import BacktestEngine
        from risk.risk_manager import RiskManager

        db.init_tables()
        start = sys.argv[2] if len(sys.argv) > 2 else "2020-01-01"
        end = sys.argv[3] if len(sys.argv) > 3 else "2024-12-31"

        print(f"=== 多因子策略回测: {start} ~ {end} ===")

        # 生成选股信号
        strategy = MultiFactorStrategy(db)
        signals = strategy.generate_signals(start, end)

        if not signals:
            print("无有效信号，回测终止")
            sys.exit(1)

        # 风控调整
        rm = RiskManager(db)
        adjusted_signals = {}
        for dt, df_sig in signals.items():
            adjusted = rm.adjust_weights(df_sig, dt)
            adjusted_signals[dt] = adjusted

        # 查询股票名称映射
        import pandas as pd
        all_codes = set()
        for df_sig in adjusted_signals.values():
            all_codes.update(df_sig["ts_code"].tolist())
        codes_str = "','".join(all_codes)
        name_map = {}
        if codes_str:
            df_names = db.query(
                f"SELECT ts_code, name FROM stock_basic WHERE ts_code IN ('{codes_str}')"
            )
            name_map = dict(zip(df_names["ts_code"], df_names["name"]))

        # 输出每期选股明细
        for dt in sorted(adjusted_signals.keys()):
            raw = signals[dt]
            adj = adjusted_signals[dt]
            merged = raw[["ts_code", "score"]].merge(
                adj[["ts_code", "weight"]], on="ts_code", how="right"
            ).sort_values("weight", ascending=False)
            print(f"\n--- 调仓日: {dt} | 选中 {len(merged)} 只 ---")
            for _, r in merged.iterrows():
                score = f"{r['score']:.3f}" if r["score"] == r["score"] else "N/A"
                name = name_map.get(r["ts_code"], "")
                print(f"  {r['ts_code']}  {name:<6s}  权重={r['weight']:.2%}  得分={score}")

        # 执行回测
        engine = BacktestEngine(db)
        result = engine.run(adjusted_signals, start, end)

        if not result:
            print("回测失败")
            sys.exit(1)

        # 输出交易记录
        trades = result.get("trades")
        if trades is not None and not trades.empty:
            print(f"\n=== 交易记录: 共 {len(trades)} 笔 ===")
            for dt, grp in trades.groupby("date"):
                buys = grp[grp["direction"] == "BUY"]
                sells = grp[grp["direction"] == "SELL"]
                print(f"\n  {dt}:")
                if not sells.empty:
                    sell_names = [f"{c}({name_map.get(c,'')})" for c in sells["ts_code"]]
                    print(f"    卖出 {len(sells)} 只: " + " ".join(sell_names))
                if not buys.empty:
                    buy_names = [f"{c}({name_map.get(c,'')})" for c in buys["ts_code"]]
                    print(f"    买入 {len(buys)} 只: " + " ".join(buy_names))

        # 输出每日净值
        nav = result.get("nav")
        if nav is not None and not nav.empty:
            print("\n=== 每日净值 ===")
            for dt, v in nav.items():
                print(f"  {dt.strftime('%Y-%m-%d')}  {v:.4f}")

        # 输出绩效摘要
        summary = engine.summary(result)
        print("\n=== 绩效摘要 ===")
        print(summary.to_string(index=False))

        # 保存图表
        engine.plot(result)
        print("\n回测图表已保存到 output/ 目录")

    elif command == "select":
        from strategy.multi_factor import MultiFactorStrategy

        db.init_tables()
        target = sys.argv[2] if len(sys.argv) > 2 else None
        if target is None:
            target = db.get_latest_trade_date()

        print(f"=== 全市场分行业选股: {target} ===")
        strategy = MultiFactorStrategy(db)
        scored = strategy.score_all_stocks(target)

        if scored.empty:
            print("选股结果为空")
        else:
            # 查询股票名称
            codes_str = "','".join(scored["ts_code"].tolist())
            df_names = db.query(
                f"SELECT ts_code, name FROM stock_basic WHERE ts_code IN ('{codes_str}')"
            )
            scored = scored.merge(df_names, on="ts_code", how="left")

            # 如果 score_all_stocks 没有返回行业列，补充行业信息
            if "industry_name" not in scored.columns:
                try:
                    df_ind = db.query(
                        f"SELECT ts_code, industry_name FROM industry_class WHERE ts_code IN ('{codes_str}')"
                    )
                    scored = scored.merge(df_ind, on="ts_code", how="left")
                except Exception:
                    pass

            if "industry_name" not in scored.columns:
                scored["industry_name"] = "未知"

            scored["industry_name"] = scored["industry_name"].fillna("未知")

            # 每个行业取 top 5
            top5 = (
                scored.sort_values("score", ascending=False)
                .groupby("industry_name")
                .head(5)
            )

            # 行业排行：按该行业最高分降序
            ind_max = top5.groupby("industry_name")["score"].max().sort_values(ascending=False)

            total_industries = len(ind_max)
            total_stocks = len(top5)
            print(f"\n共 {total_industries} 个行业，{total_stocks} 只股票（每行业 Top 5）\n")

            rank = 0
            for ind_name in ind_max.index:
                rank += 1
                ind_stocks = top5[top5["industry_name"] == ind_name].sort_values("score", ascending=False)
                best_score = ind_stocks["score"].iloc[0]
                print(f"{'─' * 50}")
                print(f"#{rank}  {ind_name}  (最高分: {best_score:.3f})")
                print(f"{'─' * 50}")
                for i, (_, r) in enumerate(ind_stocks.iterrows(), 1):
                    name = r.get("name", "")
                    print(f"  {i}. {r['ts_code']}  {name:<6s}  得分={r['score']:.3f}")
                print()

    # ==============================================================
    # 模拟盘
    # ==============================================================
    elif command == "trade":
        import pandas as pd
        from strategy.multi_factor import MultiFactorStrategy
        from risk.risk_manager import RiskManager

        db.init_tables()
        trader = _create_trader(db)

        print("=== 执行模拟盘交易（T+1执行）===")

        # 获取最近两个交易日：信号日(T) 和 执行日(T+1)
        # 工作流：update_all 下载 T+1 数据后运行 trade
        df_dates = db.query(
            "SELECT DISTINCT trade_date FROM daily_price "
            "ORDER BY trade_date DESC LIMIT 2"
        )
        if df_dates.empty:
            print("无行情数据，请先运行 update_all")
            sys.exit(1)

        dates = sorted(
            pd.to_datetime(df_dates["trade_date"]).dt.strftime("%Y-%m-%d").tolist()
        )

        if len(dates) >= 2:
            signal_date = dates[-2]  # T（前一交易日）
            exec_date = dates[-1]    # T+1（最新交易日）
        else:
            signal_date = dates[-1]
            exec_date = dates[-1]
            logger.warning("仅有一个交易日数据，信号与执行使用同一天")

        print(f"  信号日: {signal_date}（T日收盘后产生信号）")
        print(f"  执行日: {exec_date}（T+1日开盘价成交）")

        # 基于 T 日数据生成选股信号
        strategy = MultiFactorStrategy(db)
        signal = strategy.select_stocks(signal_date)

        if signal.empty:
            print(f"选股日 {signal_date} 无有效信号")
            sys.exit(1)

        # 风控调整
        rm = RiskManager(db)
        adjusted = rm.adjust_weights(signal, signal_date)

        print(f"  选中 {len(adjusted)} 只股票")

        # 在 T+1 日执行（使用 T+1 开盘价）
        result = trader.sync_position(adjusted, trade_date=exec_date)
        print(f"交易完成: {result}")

    elif command == "position":
        db.init_tables()
        trader = _create_trader(db)
        print("=== 模拟盘持仓 ===")
        print(trader.get_position_report())

    elif command == "paper_replay":
        from strategy.multi_factor import MultiFactorStrategy
        from risk.risk_manager import RiskManager
        from execution.paper_trader import PaperTrader

        db.init_tables()
        start = sys.argv[2] if len(sys.argv) > 2 else "2020-01-01"
        end = sys.argv[3] if len(sys.argv) > 3 else "2024-12-31"

        # 检查 --reset 和 --capital 参数
        capital = PAPER_INITIAL_CAPITAL
        reset = False
        for arg in sys.argv[4:]:
            if arg == "--reset":
                reset = True
            elif arg.startswith("--capital"):
                capital = float(arg.split("=")[1] if "=" in arg else sys.argv[sys.argv.index(arg) + 1])

        trader = PaperTrader(db)
        trader.connect(initial_capital=capital)

        if reset:
            print("重置模拟账户...")
            trader.reset_account()

        print(f"=== 模拟盘回放: {start} ~ {end} ===")

        # 生成信号
        strategy = MultiFactorStrategy(db)
        signals = strategy.generate_signals(start, end)

        if not signals:
            print("无有效信号")
            sys.exit(1)

        # 风控调整
        rm = RiskManager(db)
        adjusted_signals = {}
        for dt, df_sig in signals.items():
            adjusted_signals[dt] = rm.adjust_weights(df_sig, dt)

        # 回放
        trader.replay(adjusted_signals, start, end)

        # 输出净值摘要
        nav_series = trader.get_nav_series()
        if not nav_series.empty:
            from strategy.backtest import BacktestEngine
            summary = BacktestEngine.summary({"nav": nav_series})
            print("\n=== 绩效摘要 ===")
            print(summary.to_string(index=False))

        print(f"\n{trader.get_position_report()}")

    elif command == "paper_nav":
        from execution.paper_trader import PaperTrader

        db.init_tables()
        trader = PaperTrader(db)
        trader.connect()

        df = trader.get_nav_history(last_n=30)
        if df.empty:
            print("暂无净值记录")
        else:
            print("=== 模拟盘净值历史（最近30天）===")
            print(df.to_string(index=False))

    elif command == "paper_transactions":
        from execution.paper_trader import PaperTrader

        db.init_tables()
        trader = PaperTrader(db)
        trader.connect()

        last_n = 50
        trade_date = None
        for arg in sys.argv[2:]:
            if arg.startswith("--last"):
                last_n = int(arg.split("=")[1] if "=" in arg else sys.argv[sys.argv.index(arg) + 1])
            elif arg.startswith("--date"):
                trade_date = arg.split("=")[1] if "=" in arg else sys.argv[sys.argv.index(arg) + 1]

        df = trader.get_transactions(trade_date=trade_date, last_n=last_n)
        if df.empty:
            print("暂无交易记录")
        else:
            print(f"=== 模拟盘交易记录（最近{last_n}条）===")
            print(df.to_string(index=False))

    elif command == "paper_reset":
        from execution.paper_trader import PaperTrader

        db.init_tables()

        if "--confirm" not in sys.argv:
            print("此操作将清空模拟账户的所有持仓、交易记录和净值历史！")
            print("请添加 --confirm 参数确认执行")
            sys.exit(1)

        trader = PaperTrader(db)
        trader.connect()
        trader.reset_account()
        print("模拟账户已重置")

    # ==============================================================
    # 报告生成
    # ==============================================================
    elif command == "report":
        from strategy.multi_factor import MultiFactorStrategy
        from strategy.backtest import BacktestEngine
        from risk.risk_manager import RiskManager
        from monitor.performance import PerformanceAnalyzer
        from monitor.report import ReportGenerator

        db.init_tables()
        start = sys.argv[2] if len(sys.argv) > 2 else "2020-01-01"
        end = sys.argv[3] if len(sys.argv) > 3 else "2024-12-31"

        print(f"=== 生成策略报告: {start} ~ {end} ===")

        # 运行回测
        strategy = MultiFactorStrategy(db)
        signals = strategy.generate_signals(start, end)

        if not signals:
            print("无有效信号")
            sys.exit(1)

        rm = RiskManager(db)
        adjusted_signals = {}
        for dt, df_sig in signals.items():
            adjusted_signals[dt] = rm.adjust_weights(df_sig, dt)

        engine = BacktestEngine(db)
        result = engine.run(adjusted_signals, start, end)

        if not result:
            print("回测失败")
            sys.exit(1)

        # 生成报告
        nav = result["nav"]
        benchmark = result.get("benchmark_nav")

        # 获取最新持仓
        latest_signal_date = sorted(adjusted_signals.keys())[-1]
        holdings = adjusted_signals[latest_signal_date]

        # 行业归因
        analyzer = PerformanceAnalyzer(db)
        attribution = analyzer.industry_attribution(holdings, start, end)

        gen = ReportGenerator()
        html = gen.generate(
            nav=nav,
            benchmark_nav=benchmark,
            holdings=holdings,
            industry_attribution=attribution,
        )
        filepath = gen.save(html)
        print(f"\n报告已生成: {filepath}")

    # ==============================================================
    # 行业因子配置
    # ==============================================================
    elif command == "seed_industry_config":
        from data.seed_config import get_seed_records
        db.init_tables()
        records = get_seed_records()
        db.upsert_industry_factor_config(records)
        print(f"=== 行业因子权重种子数据已写入 ===")
        print(f"共 {len(records)} 条记录（可重复运行，upsert 语义）")

    elif command == "show_industry_config":
        db.init_tables()
        df = db.get_industry_factor_weights()
        if df.empty:
            print("行业因子权重配置为空，请先运行: python3 main.py seed_industry_config")
        else:
            print("=== 行业因子权重配置 ===")
            for industry, grp in df.groupby("industry_name"):
                print(f"\n  [{industry}]")
                for _, row in grp.sort_values("factor_name").iterrows():
                    print(f"    {row['factor_name']:20s} = {row['weight']:+.1f}")

    # ==============================================================
    # 舆情抓取
    # ==============================================================
    elif command in ("download_sentiment", "update_sentiment"):
        from sentiment.downloader import SentimentDownloader
        from sentiment.scrapers import SCRAPER_REGISTRY, TIER_MAP

        db.init_tables()
        dl = SentimentDownloader(db)

        # 解析参数
        source = None
        tier = None
        for arg in sys.argv[2:]:
            if arg.startswith("--source="):
                source = arg.split("=", 1)[1]
            elif arg.startswith("--tier="):
                tier = int(arg.split("=", 1)[1])

        if source:
            print(f"=== 抓取舆情: {source} ===")
            result = dl.download_source(source)
            print(f"  {source}: 发现 {result['found']} 篇，新增 {result['new']} 篇 [{result['status']}]")
        elif tier:
            tier_names = {1: "最高层", 2: "产业层", 3: "金融监管", 4: "专项行业"}
            print(f"=== 抓取舆情: 层级 {tier} ({tier_names.get(tier, '未知')}) ===")
            results = dl.download_tier(tier)
            for src, r in results.items():
                print(f"  {src:10s}: 发现 {r['found']:3d} 篇，新增 {r['new']:3d} 篇 [{r['status']}]")
        else:
            print("=== 全量抓取舆情: 11 个政府网站 ===")
            results = dl.download_all()
            total_found = total_new = 0
            for src, r in results.items():
                print(f"  {src:10s}: 发现 {r['found']:3d} 篇，新增 {r['new']:3d} 篇 [{r['status']}]")
                total_found += r["found"]
                total_new += r["new"]
            print(f"\n  合计: 发现 {total_found} 篇，新增 {total_new} 篇")

    elif command == "sentiment_status":
        db.init_tables()
        print("=== 舆情数据状态 ===")
        try:
            df = db.query(
                "SELECT source, COUNT(*) as cnt, "
                "MIN(publish_date) as earliest, MAX(publish_date) as latest "
                "FROM policy_article GROUP BY source ORDER BY source"
            )
            if df.empty:
                print("  暂无舆情数据，请先运行: python3 main.py download_sentiment")
            else:
                tier_map = {
                    "gov_cn": 1, "xinhua": 1, "people": 1,
                    "ndrc": 2, "miit": 2, "mofcom": 2,
                    "csrc": 3, "pbc": 3, "nfra": 3,
                    "nea": 4, "mohurd": 4,
                }
                tier_names = {1: "最高层", 2: "产业层", 3: "金融监管", 4: "专项行业"}
                total = 0
                for _, row in df.iterrows():
                    t = tier_map.get(row["source"], 0)
                    tn = tier_names.get(t, "?")
                    print(
                        f"  {row['source']:10s} [T{t}-{tn}]: "
                        f"{row['cnt']:5d} 篇  ({row['earliest']} ~ {row['latest']})"
                    )
                    total += row["cnt"]
                print(f"\n  合计: {total} 篇")
        except Exception as e:
            print(f"  查询失败: {e}")
            print("  请先运行: python3 main.py init")

    # ==============================================================
    # 状态查看
    # ==============================================================
    elif command == "status":
        db.init_tables()
        print("=== 数据库状态 ===")
        tables = [
            ("stock_basic", "股票基本信息"),
            ("daily_price", "日线行情"),
            ("financial_data", "财务数据"),
            ("industry_class", "行业分类"),
            ("paper_account", "模拟盘账户"),
            ("paper_position", "模拟盘持仓"),
            ("paper_transaction", "模拟盘交易"),
            ("paper_nav", "模拟盘净值"),
            ("industry_factor_config", "行业因子配置"),
            ("policy_article", "政策文章"),
            ("scrape_log", "抓取日志"),
        ]
        for table_name, label in tables:
            try:
                count = db.table_count(table_name)
                extra = ""
                if table_name == "daily_price":
                    latest = db.get_latest_trade_date()
                    extra = f"，最新: {latest}"
                print(f"  {label:10s}: {count:>10,} 条{extra}")
            except Exception:
                print(f"  {label:10s}: 表为空或不存在")

    else:
        print(f"未知命令: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
