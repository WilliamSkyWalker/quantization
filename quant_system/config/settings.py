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
EXCLUDE_STAR_MARKET = os.environ.get("EXCLUDE_STAR_MARKET", "1") == "1"  # 排除科创板（688）

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
MIN_HOLDINGS = 0                  # 最少持仓数，0 = 允许空仓
MAX_HOLDINGS = int(os.environ.get("MAX_HOLDINGS", "10"))
MIN_SELECT_SCORE = float(os.environ.get("MIN_SELECT_SCORE", "0"))  # 选股最低分，低于此分不入选
MAX_SINGLE_WEIGHT = 0.05
MAX_INDUSTRY_WEIGHT = 0.30
MAX_DRAWDOWN_THRESHOLD = 0.15
DRAWDOWN_REDUCE_POSITION = 0.50
MIN_DAILY_TURNOVER = 50_000_000

# 换手惩罚系数（0.0 = 关闭）
TURNOVER_PENALTY_LAMBDA = float(os.environ.get("TURNOVER_PENALTY_LAMBDA", "0.0"))

# 中性化模式: "full" / "size_only" / "none"
NEUTRALIZE_MODE = os.environ.get("NEUTRALIZE_MODE", "full")
# 非线性市值项: 0 = 关闭, 1 = 开启
NONLINEAR_SIZE = os.environ.get("NONLINEAR_SIZE", "0") == "1"

# 波动率目标管理（替代回撤缩仓）
USE_VOL_TARGETING = os.environ.get("USE_VOL_TARGETING", "0") == "1"
TARGET_VOL = float(os.environ.get("TARGET_VOL", "0.20"))
VOL_LOOKBACK_DAYS = int(os.environ.get("VOL_LOOKBACK_DAYS", "20"))
VOL_SCALE_MIN = float(os.environ.get("VOL_SCALE_MIN", "0.3"))
VOL_SCALE_MAX = float(os.environ.get("VOL_SCALE_MAX", "1.0"))

# 行业白名单（仅允许买入的行业，空列表=不限制）
# .env 中用逗号分隔，例如: ALLOWED_INDUSTRIES=计算机,半导体,有色金属,基础化工
_raw_industries = os.environ.get("ALLOWED_INDUSTRIES", "")
ALLOWED_INDUSTRIES: list[str] = [s.strip() for s in _raw_industries.split(",") if s.strip()]

# 行业指数映射（行业名→申万行业指数代码，用于回测图中显示行业指数走势对比）
# .env 中格式: INDUSTRY_INDEX_MAP=有色金属:801050.SI,半导体:801081.SI,计算机:801750.SI,基础化工:801030.SI
_raw_index_map = os.environ.get("INDUSTRY_INDEX_MAP", "")
INDUSTRY_INDEX_MAP: dict[str, str] = {}
for _pair in _raw_index_map.split(","):
    _pair = _pair.strip()
    if ":" in _pair:
        _k, _v = _pair.split(":", 1)
        if _k.strip() and _v.strip():
            INDUSTRY_INDEX_MAP[_k.strip()] = _v.strip()

# ============================================================
# 模拟盘配置
# ============================================================

PAPER_INITIAL_CAPITAL = float(os.environ.get("PAPER_INITIAL_CAPITAL", "1000000"))
PAPER_ACCOUNT_NAME = os.environ.get("PAPER_ACCOUNT_NAME", "default")
TRADER_TYPE = os.environ.get("TRADER_TYPE", "paper")  # paper / qmt / ptrade

# ============================================================
# 舆情抓取配置
# ============================================================

SENTIMENT_RATE_LIMIT = int(os.environ.get("SENTIMENT_RATE_LIMIT", "20"))          # 每分钟每域名请求数
SENTIMENT_REQUEST_TIMEOUT = int(os.environ.get("SENTIMENT_REQUEST_TIMEOUT", "30"))  # 请求超时（秒）
SENTIMENT_MAX_RETRIES = int(os.environ.get("SENTIMENT_MAX_RETRIES", "3"))
SENTIMENT_RETRY_WAIT = int(os.environ.get("SENTIMENT_RETRY_WAIT", "5"))            # 重试等待（秒）
SENTIMENT_MAX_PAGES = int(os.environ.get("SENTIMENT_MAX_PAGES", "5"))              # 每来源最大翻页数
SENTIMENT_USER_AGENT = os.environ.get(
    "SENTIMENT_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)

# ============================================================
# 日志配置
# ============================================================

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
