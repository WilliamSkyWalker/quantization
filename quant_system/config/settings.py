"""
全局配置文件

优先从项目根目录的 .env 文件加载配置，未设置的项使用默认值。
修改配置请编辑 .env 文件，不要直接改此文件。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# ============================================================
# 加载 .env
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# 日志目录
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ============================================================
# MySQL 数据库配置
# ============================================================

MYSQL_HOST = os.environ.get("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("MYSQL_USER", "root")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "123456")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "quant")

DB_URL = (
    f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}"
    f"@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}"
    f"?charset=utf8mb4"
)

# ============================================================
# API 配置
# ============================================================

TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
TUSHARE_RATE_LIMIT = int(os.environ.get("TUSHARE_RATE_LIMIT", "180"))       # 每分钟请求数
TUSHARE_RETRY_WAIT = int(os.environ.get("TUSHARE_RETRY_WAIT", "15"))        # 触发限流后等待秒数
TUSHARE_MAX_RETRIES = int(os.environ.get("TUSHARE_MAX_RETRIES", "3"))       # 最大重试次数

# ============================================================
# 数据参数
# ============================================================

DATA_START_DATE = os.environ.get("DATA_START_DATE", "20150101")
IPO_FILTER_DAYS = 180

# ============================================================
# 市场参数
# ============================================================

ST_KEYWORDS = ["ST", "*ST", "S*ST", "SST"]

# ============================================================
# 交易成本假设
# ============================================================

BUY_COMMISSION = 0.00075   # 买入佣金 万7.5
SELL_COMMISSION = 0.00075  # 卖出佣金 万7.5
STAMP_TAX = 0.001          # 印花税 0.1%（仅卖出）
SLIPPAGE = 0.001           # 滑点 0.1%

# ============================================================
# 策略参数
# ============================================================

REBALANCE_FREQ = "M"
MIN_HOLDINGS = 20
MAX_HOLDINGS = 30
MAX_SINGLE_WEIGHT = 0.05
MAX_INDUSTRY_WEIGHT = 0.30
MAX_DRAWDOWN_THRESHOLD = 0.15
DRAWDOWN_REDUCE_POSITION = 0.50
MIN_DAILY_TURNOVER = 50_000_000

# ============================================================
# 模拟盘配置
# ============================================================

PAPER_INITIAL_CAPITAL = float(os.environ.get("PAPER_INITIAL_CAPITAL", "1000000"))
PAPER_ACCOUNT_NAME = os.environ.get("PAPER_ACCOUNT_NAME", "default")
TRADER_TYPE = os.environ.get("TRADER_TYPE", "paper")  # paper / qmt / ptrade

# ============================================================
# 日志配置
# ============================================================

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
