# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

A股多因子量化选股系统，覆盖数据采集、因子计算、组合构建、风控、回测、模拟交易、舆情爬取和报告生成的完整流程。

所有代码在 `quant_system/` 目录下，无构建系统（无 setup.py、pyproject.toml、Makefile）。

## 常用命令

所有命令在 `quant_system/` 目录下执行：

```bash
cd quant_system

# 安装依赖
pip install -r requirements.txt

# 初始化数据库
python3 main.py init

# 数据管道
python3 main.py download_all          # 股票列表 + OHLCV + 沪深300指数
python3 main.py download_extra        # 财务数据 + 估值 + 行业分类
python3 main.py backfill_income       # 补充利润表（营收/净利润）
python3 main.py update_all            # 增量更新

# 策略
python3 main.py select 2025-01-31     # 单日选股
python3 main.py backtest 2020-01-01 2024-12-31

# 模拟交易
python3 main.py trade                 # 执行 T+1 信号
python3 main.py position              # 查看持仓
python3 main.py paper_replay 2020-01-01 2024-12-31 --reset

# 舆情爬取
python3 main.py download_sentiment
python3 main.py sentiment_status

# 报告
python3 main.py report 2020-01-01 2024-12-31

# 测试
pytest                                # 全部测试（在 quant_system/ 下运行）
pytest tests/test_strategy.py -v      # 单个文件
pytest -x                             # 遇到失败即停止
```

测试使用 SQLite 内存数据库，无需外部 MySQL。

## 系统架构

```
Tushare API → data/{downloader,updater}.py → MySQL（10 张 ORM 表）
  → data/cleaner.py（股票池过滤：退市、ST、上市天数、停牌、流动性、科创板）
  → factors/*.py（19 个因子，5 大类，均继承 FactorBase ABC）
  → factors/processor.py（MAD 去极值 → OLS 中性化 → Z-score 标准化 → 截断 ±3）
  → strategy/multi_factor.py（两层打分 → Top-N 选股 → 按得分分配权重）
  → risk/risk_manager.py（个股/行业上限 → 回撤控制 / 波动率目标）
  → strategy/backtest.py 或 execution/paper_trader.py
  → monitor/{performance,report}.py
```

**舆情管道：** `sentiment/scrapers/` 下 11 个中国政府网站爬虫 + 3 个 Twitter/X 美国政策爬虫（Trump/Vance/Rubio），由 `sentiment/downloader.py` 调度，`HttpRateLimiter` 实现按域名限速。Twitter 爬虫使用 twikit 库（免费，需 `TWITTER_USERNAME`/`TWITTER_EMAIL`/`TWITTER_PASSWORD`），独立限速器（90 req/min），缺少凭证或 twikit 未安装时优雅降级（跳过，不报错）。

### 核心设计决策

- **无未来数据泄露：** 财务数据始终按 `ann_date <= date`（公告日）过滤，而非报告期。收盘价和市值取信号日当天数据。
- **两层因子打分：** 类内使用动态分母（缺失因子等比缩减权重）；类间使用固定分母（4.5），缺失大类贡献为 0，不重新分配权重。
- **Upsert 语义：** 所有数据库写入为幂等操作（唯一键冲突时 insert-or-update）。
- **可配置中性化：** `NEUTRALIZE_MODE = full | size_only | none` 控制 OLS 行业+市值残差中性化，使用 `numpy.linalg.pinv`（伪逆）保证数值稳定性。
- **T+1 执行模型：** 先卖后买。涨停股排除在买入之外；跌停股加入 `pending_sells` 队列下一交易日重试。
- **可插拔交易后端：** `BaseTrader` ABC + `main.py::_create_trader()` 工厂方法，目前仅实现 `PaperTrader`。

### 因子体系（19 个因子）

| 大类 | 权重 | 因子 |
|---|---|---|
| 价值 | 1.0 | EP, BP |
| 质量 | 1.0 | ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND |
| 成长 | 1.2 | NET_PROFIT_YOY, REVENUE_YOY |
| 动量 | 1.3 | MOM_1M, MOM_3M, MOM_12M, REV_5D, IND_MOM, RESIDUAL_MOM |
| 技术 | 0.5 | TURN_20D, VOL_20D, PRICE_DEV_60D, SIZE, VOL_PRICE_DIV |

## 配置

环境变量在 `quant_system/.env`（参考 `.env.example`）。关键配置：`TUSHARE_TOKEN`、`TWITTER_USERNAME`/`TWITTER_EMAIL`/`TWITTER_PASSWORD`（twikit 免费方案）、MySQL 连接信息、`MAX_HOLDINGS`、`NEUTRALIZE_MODE`、`USE_VOL_TARGETING`、风控参数。所有配置在 `config/settings.py` 中有默认值。

## 编码规范

- 所有模块使用 `logging.getLogger(__name__)`，日志级别取 `config.settings.LOG_LEVEL`
- Matplotlib 必须在导入 `pyplot` 前调用 `matplotlib.use("Agg")`
- 因子计算始终为截面（同一日期，全部股票）
- MySQL 列名 `open` 是保留字，原生 SQL 需用反引号转义
- 数据库层使用 SQLAlchemy ORM（`DeclarativeBase`）
- 面向 A 股市场（申万行业分类、涨跌停处理、T+1 规则）
- 在[ALGORITHM.md](quant_system/ALGORITHM.md) 和[CONTINUE_PROMPT.md](quant_system/CONTINUE_PROMPT.md)中记录变动
