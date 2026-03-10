"""
全局配置文件

优先从项目根目录的 .env 文件加载配置，未设置的项使用默认值。
修改配置请编辑 .env 文件，不要直接改此文件。
"""

import json as _json
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
MAX_HOLDINGS = int(os.environ.get("MAX_HOLDINGS", "15"))
MIN_SELECT_SCORE = float(os.environ.get("MIN_SELECT_SCORE", "0"))  # 选股最低分，低于此分不入选
MAX_SINGLE_WEIGHT = float(os.environ.get("MAX_SINGLE_WEIGHT", "0.12"))
MAX_INDUSTRY_WEIGHT = float(os.environ.get("MAX_INDUSTRY_WEIGHT", "0.20"))

# 关联行业组合上限（同一产业链合计不超过此值）
MAX_INDUSTRY_GROUP_WEIGHT = float(os.environ.get("MAX_INDUSTRY_GROUP_WEIGHT", "0.30"))
# 关联行业分组定义
INDUSTRY_GROUPS: dict[str, list[str]] = {
    "地产链": ["房地产", "建筑装饰", "建筑材料"],
    "金融": ["银行", "非银金融"],
    "TMT": ["计算机", "电子", "通信", "传媒"],
}
MAX_DRAWDOWN_THRESHOLD = 0.25
DRAWDOWN_REDUCE_POSITION = 0.70
# 线性回撤响应参数
DD_START_THRESHOLD = float(os.environ.get("DD_START_THRESHOLD", "0.07"))
DD_MAX_THRESHOLD = float(os.environ.get("DD_MAX_THRESHOLD", "0.20"))
DD_MIN_POSITION = float(os.environ.get("DD_MIN_POSITION", "0.40"))
MIN_DAILY_TURNOVER = 50_000_000
MIN_MARKET_CAP = float(os.environ.get("MIN_MARKET_CAP", "3e9"))  # 最低总市值（元），默认 30 亿

# 换手惩罚系数（0.0 = 关闭）
TURNOVER_PENALTY_LAMBDA = float(os.environ.get("TURNOVER_PENALTY_LAMBDA", "0.15"))

# 最小有效大类数（有效大类数低于此值时股票得分设为 NaN，被剔除）
MIN_VALID_CATEGORIES = int(os.environ.get("MIN_VALID_CATEGORIES", "4"))

# 中性化模式: "full" / "size_only" / "none"
NEUTRALIZE_MODE = os.environ.get("NEUTRALIZE_MODE", "full")
# 非线性市值项: 0 = 关闭, 1 = 开启
NONLINEAR_SIZE = os.environ.get("NONLINEAR_SIZE", "0") == "1"

# 按大类覆盖中性化模式（JSON 格式，例如 '{"macro":"size_only","sentiment":"none"}'）
_raw_cat_neutralize = os.environ.get("CATEGORY_NEUTRALIZE_OVERRIDES", "")
CATEGORY_NEUTRALIZE_OVERRIDES: dict[str, str] = {}
if _raw_cat_neutralize.strip():
    try:
        CATEGORY_NEUTRALIZE_OVERRIDES = _json.loads(_raw_cat_neutralize)
    except _json.JSONDecodeError:
        pass
# 默认: momentum/macro/sentiment 使用 size_only，保留行业 alpha
# momentum: IND_MOM/CMDTY_MOM 本质是行业级信号，full 中性化会回归掉行业效应导致信号归零
if "momentum" not in CATEGORY_NEUTRALIZE_OVERRIDES:
    CATEGORY_NEUTRALIZE_OVERRIDES["momentum"] = "size_only"
if "macro" not in CATEGORY_NEUTRALIZE_OVERRIDES:
    CATEGORY_NEUTRALIZE_OVERRIDES["macro"] = "size_only"
if "sentiment" not in CATEGORY_NEUTRALIZE_OVERRIDES:
    CATEGORY_NEUTRALIZE_OVERRIDES["sentiment"] = "size_only"

# 标准化模式: "zscore" | "rank"（rank percentile 对偏态分布更稳健）
STANDARDIZE_MODE = os.environ.get("STANDARDIZE_MODE", "zscore")

# Softmax 权重温度参数（τ > 0: softmax, τ = 0: 等权）
WEIGHT_TEMPERATURE = float(os.environ.get("WEIGHT_TEMPERATURE", "2.0"))

# Regime 切换（CSI 300 120 日均线判定牛/熊）
REGIME_ENABLED = os.environ.get("REGIME_ENABLED", "1") == "1"
REGIME_MA_WINDOW = int(os.environ.get("REGIME_MA_WINDOW", "60"))
REGIME_INDEX_CODE = os.environ.get("REGIME_INDEX_CODE", "000300.SH")
# 熊市大类权重覆盖
_raw_regime_bear = os.environ.get("REGIME_BEAR_OVERRIDES", "")
REGIME_BEAR_OVERRIDES: dict[str, float] = {
    "momentum": 0.6, "quality": 1.5, "growth": 0.8, "value": 0.6, "technical": 1.0,
}
if _raw_regime_bear.strip():
    try:
        REGIME_BEAR_OVERRIDES = {k: float(v) for k, v in _json.loads(_raw_regime_bear).items()}
    except Exception:
        pass
# 牛市大类权重覆盖（强牛时提升动量/成长，降低质量防守）
_raw_regime_bull = os.environ.get("REGIME_BULL_OVERRIDES", "")
REGIME_BULL_OVERRIDES: dict[str, float] = {
    "momentum": 1.2, "quality": 0.9, "growth": 1.2, "value": 0.5, "technical": 0.6,
}
if _raw_regime_bull.strip():
    try:
        REGIME_BULL_OVERRIDES = {k: float(v) for k, v in _json.loads(_raw_regime_bull).items()}
    except Exception:
        pass

# 波动率目标管理（替代回撤缩仓）
USE_VOL_TARGETING = os.environ.get("USE_VOL_TARGETING", "1") == "1"
TARGET_VOL = float(os.environ.get("TARGET_VOL", "0.16"))
VOL_LOOKBACK_DAYS = int(os.environ.get("VOL_LOOKBACK_DAYS", "60"))
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
# 券商研报配置
RESEARCH_LOOKBACK_DAYS = int(os.environ.get("RESEARCH_LOOKBACK_DAYS", "90"))

# 舆情抓取配置
# ============================================================

SENTIMENT_RATE_LIMIT = int(os.environ.get("SENTIMENT_RATE_LIMIT", "600"))         # 每分钟每域名请求数
SENTIMENT_REQUEST_TIMEOUT = int(os.environ.get("SENTIMENT_REQUEST_TIMEOUT", "30"))  # 请求超时（秒）
SENTIMENT_MAX_RETRIES = int(os.environ.get("SENTIMENT_MAX_RETRIES", "3"))
SENTIMENT_RETRY_WAIT = int(os.environ.get("SENTIMENT_RETRY_WAIT", "5"))            # 重试等待（秒）
SENTIMENT_MAX_PAGES = int(os.environ.get("SENTIMENT_MAX_PAGES", "5"))              # 每来源最大翻页数
SENTIMENT_BACKFILL_DAYS = int(os.environ.get("SENTIMENT_BACKFILL_DAYS", "1095"))  # 全量补录回看天数（CCTV/巨潮），默认 3 年
SENTIMENT_USER_AGENT = os.environ.get(
    "SENTIMENT_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)

# ============================================================
# Twitter/X 配置（twikit 免费方案，使用普通账号登录）
# ============================================================

TWITTER_USERNAME = os.environ.get("TWITTER_USERNAME", "")
TWITTER_EMAIL = os.environ.get("TWITTER_EMAIL", "")
TWITTER_PASSWORD = os.environ.get("TWITTER_PASSWORD", "")
TWITTER_COOKIES_FILE = str(PROJECT_ROOT / os.environ.get("TWITTER_COOKIES_FILE", "twitter_cookies.json"))
TWITTER_RATE_LIMIT = int(os.environ.get("TWITTER_RATE_LIMIT", "90"))   # req/min
TWITTER_MAX_TWEETS = int(os.environ.get("TWITTER_MAX_TWEETS", "40"))   # 每页最大推文数（twikit 上限 40）

# ============================================================
# 商品期货配置（商品价格轮动因子）
# ============================================================

COMMODITY_SYMBOLS = [
    "AU", "AG", "CU", "AL", "ZN", "PB", "NI", "SN",  # 有色金属
    "RB", "I", "J", "JM",                              # 黑色系
    "SC",                                               # 能源
    "SA", "MA",                                         # 化工
]

COMMODITY_MOM_LOOKBACK = int(os.environ.get("COMMODITY_MOM_LOOKBACK", "60"))  # 动量回看窗口（交易日），60日捕捉中期商品趋势
COMMODITY_SURGE_ZSCORE = float(os.environ.get("COMMODITY_SURGE_ZSCORE", "2.0"))   # 暴涨判定阈值（动量 z-score >= 此值触发放大）
COMMODITY_SURGE_MULTIPLIER = float(os.environ.get("COMMODITY_SURGE_MULTIPLIER", "1.5"))  # 暴涨最大放大倍数（z=3 时达到此倍数）
COMMODITY_SURGE_LOOKBACK = int(os.environ.get("COMMODITY_SURGE_LOOKBACK", "500"))  # 历史动量分布回看窗口（交易日，约2年）

# 商品→行业两层映射（l2 精确匹配申万二级，l1 回退到申万一级）
COMMODITY_INDUSTRY_MAP = {
    "AU": {"l1": "有色金属", "l2": "贵金属"},
    "AG": {"l1": "有色金属", "l2": "贵金属"},
    "CU": {"l1": "有色金属", "l2": "工业金属"},
    "AL": {"l1": "有色金属", "l2": "铝"},
    "ZN": {"l1": "有色金属", "l2": "工业金属"},
    "PB": {"l1": "有色金属", "l2": "工业金属"},
    "NI": {"l1": "有色金属", "l2": "工业金属"},
    "SN": {"l1": "有色金属", "l2": "工业金属"},
    "RB": {"l1": "钢铁", "l2": "普钢"},
    "I":  {"l1": "钢铁", "l2": "特钢"},
    "J":  {"l1": "钢铁", "l2": "普钢"},
    "JM": {"l1": "钢铁", "l2": "普钢"},
    "SC": {"l1": "石油石化", "l2": "油气开采"},
    "SA": {"l1": "基础化工", "l2": "纯碱"},
    "MA": {"l1": "基础化工", "l2": "其他化学制品"},
}

# 商品品种→交易所后缀映射
COMMODITY_EXCHANGE_MAP = {
    "AU": "SHF", "AG": "SHF", "CU": "SHF", "AL": "SHF",
    "ZN": "SHF", "PB": "SHF", "NI": "SHF", "SN": "SHF",
    "RB": "SHF",
    "I": "DCE", "J": "DCE", "JM": "DCE",
    "SC": "INE",
    "SA": "ZCE", "MA": "ZCE",
}

# ============================================================
# 宏观经济数据配置（宏观因子）
# ============================================================

MACRO_ZSCORE_WINDOW = int(os.environ.get("MACRO_ZSCORE_WINDOW", "24"))  # trailing Z-score 月数窗口

# 各指标发布延迟（自然日），防止未来数据泄露
MACRO_PUBLICATION_LAG = {
    "SHIBOR_3M": 0, "SHIBOR_ON": 0,
    "LPR_1Y": 0,
    "CPI_YOY": 16, "PPI_YOY": 16, "PPI_MP_YOY": 16,
    "PMI_MFG": 1, "PMI_NEW_ORDER": 1,
    "M2_YOY": 16, "M1_YOY": 16, "M1_M2_SPREAD": 16,
    "GDP_YOY": 20,
    "UST_10Y": 0, "UST_2Y10Y": 0,
}

# --- 行业敏感度映射（正=受益，负=防御，未映射→NaN）---

MACRO_CYCLE_SENSITIVITY = {
    # 强顺周期（工业/资源/建设）
    "有色金属": 1.0, "钢铁": 1.0,
    "基础化工": 0.8, "机械设备": 0.8,
    "汽车": 0.7, "建筑材料": 0.7,
    "煤炭": 0.6, "电力设备": 0.6, "房地产": 0.6,
    "建筑装饰": 0.5, "石油石化": 0.5,
    # 适度顺周期（科技/消费/金融）
    "家用电器": 0.4, "电子": 0.4,
    "非银金融": 0.3, "交通运输": 0.3, "环保": 0.3,
    "银行": 0.2, "通信": 0.2, "计算机": 0.2, "国防军工": 0.2, "轻工制造": 0.2,
    # 中性
    "综合": 0.0, "美容护理": 0.0,
    # 弱防御
    "纺织服饰": -0.1, "农林牧渔": -0.1, "社会服务": -0.1,
    "商贸零售": -0.1, "传媒": -0.1,
    # 防御
    "食品饮料": -0.3, "医药生物": -0.3, "公用事业": -0.4,
}

MACRO_LIQD_SENSITIVITY = {
    # 强受益（高杠杆/资本密集/折现率最敏感）
    "房地产": 1.0, "非银金融": 0.9, "建筑装饰": 0.7,
    # 成长科技（长久期估值对利率高度敏感）
    "计算机": 0.6, "电子": 0.6,
    "通信": 0.5, "电力设备": 0.5, "银行": 0.5, "传媒": 0.5,
    # 消费/制造（信贷驱动）
    "医药生物": 0.4, "家用电器": 0.4, "汽车": 0.4,
    "建筑材料": 0.3, "机械设备": 0.3, "国防军工": 0.3, "环保": 0.3,
    # 一般消费/服务
    "轻工制造": 0.2, "交通运输": 0.2, "社会服务": 0.2,
    "商贸零售": 0.2, "美容护理": 0.2,
    # 弱正相关
    "食品饮料": 0.1, "农林牧渔": 0.1, "纺织服饰": 0.1, "综合": 0.1,
    # 中性
    "有色金属": 0.0, "钢铁": 0.0, "公用事业": 0.0, "基础化工": 0.0,
    # 负相关（大宗商品/能源在紧缩期反受益）
    "煤炭": -0.2, "石油石化": -0.2,
}

MACRO_INFL_SENSITIVITY = {
    # CPI 受益（下游消费/食品农业，终端价格随 CPI 上涨）
    "食品饮料": 0.8, "家用电器": 0.7, "农林牧渔": 0.6,
    "商贸零售": 0.6, "社会服务": 0.5,
    "医药生物": 0.4, "纺织服饰": 0.4,
    "美容护理": 0.3, "轻工制造": 0.2, "传媒": 0.2,
    # 中性（CPI-PPI 剪刀差对其影响不显著）
    "非银金融": 0.1, "计算机": 0.1,
    "银行": 0.0, "电子": 0.0, "通信": 0.0, "国防军工": 0.0,
    "公用事业": 0.0, "汽车": 0.0, "综合": 0.0,
    # 弱负相关（成本端受 PPI 上涨挤压）
    "交通运输": -0.1, "环保": -0.1, "电力设备": -0.1,
    "机械设备": -0.2, "建筑装饰": -0.2, "房地产": -0.2,
    "建筑材料": -0.3, "石油石化": -0.4, "煤炭": -0.4,
    # 强负相关（上游原材料生产商利润被 CPI 上涨而非 PPI 上涨侵蚀）
    "有色金属": -0.5, "基础化工": -0.5, "钢铁": -0.6,
}

MACRO_EXTR_SENSITIVITY = {
    # 强受益（高成长/长久期，美债利率下降降低折现率）
    "计算机": 0.6, "电子": 0.6, "电力设备": 0.5,
    "传媒": 0.4, "通信": 0.4, "医药生物": 0.4,
    # 适度受益
    "国防军工": 0.3, "有色金属": 0.2, "机械设备": 0.2, "环保": 0.2,
    # 弱正相关
    "非银金融": 0.1, "基础化工": 0.1, "汽车": 0.1,
    "家用电器": 0.1, "社会服务": 0.1,
    # 中性
    "农林牧渔": 0.0, "纺织服饰": 0.0, "石油石化": 0.0,
    "钢铁": 0.0, "综合": 0.0, "美容护理": 0.0,
    "商贸零售": 0.0, "轻工制造": 0.0,
    # 弱负相关（防御/高股息，利率下行反而相对吸引力下降）
    "食品饮料": -0.1, "交通运输": -0.1, "煤炭": -0.1, "建筑材料": -0.1,
    # 负相关（高杠杆/防御板块，美债利率上升时资金回流美元资产）
    "银行": -0.2, "建筑装饰": -0.2, "公用事业": -0.3, "房地产": -0.3,
}

# ============================================================
# 舆情因子配置
# ============================================================

SENTIMENT_LOOKBACK_DAYS = int(os.environ.get("SENTIMENT_LOOKBACK_DAYS", "7"))
SENTIMENT_DECAY = float(os.environ.get("SENTIMENT_DECAY", "0.3"))          # 时间衰减系数（约 3 天半衰期）
SENTIMENT_LLM_THRESHOLD = float(os.environ.get("SENTIMENT_LLM_THRESHOLD", "0.5"))  # keyword intensity 阈值
SENTIMENT_SURGE_BASELINE = int(os.environ.get("SENTIMENT_SURGE_BASELINE", "5"))     # 行业文章数基线（已废弃，改用截面 z-score）
SENTIMENT_SURGE_MULTIPLIER = float(os.environ.get("SENTIMENT_SURGE_MULTIPLIER", "1.0"))  # 舆情权重动态提升倍数（1.0=禁用，需配合更精准的数据源）
SENTIMENT_SURGE_ZSCORE = float(os.environ.get("SENTIMENT_SURGE_ZSCORE", "1.5"))     # 舆情热度 z-score 阈值（行业文章数异常时触发权重提升）
SENTIMENT_CONTENT_MAX_CHARS = int(os.environ.get("SENTIMENT_CONTENT_MAX_CHARS", "10000"))  # LLM 传入正文最大字符数

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")  # anthropic | openai
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_API_BASE = os.environ.get("LLM_API_BASE", "https://api.openai.com/v1")  # 仅 openai provider 使用
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")  # anthropic 默认 Haiku；openai 可改 gpt-4o-mini

# --- 行业关键词映射（申万一级行业 → 关键词）---
INDUSTRY_KEYWORDS = {
    "房地产": ["房地产", "住房", "楼市", "房价", "限购", "公积金", "棚改", "保障房"],
    "银行": ["银行", "存款", "贷款", "利率", "LPR", "准备金", "降息", "加息"],
    "非银金融": ["保险", "证券", "基金", "股市", "IPO", "注册制", "资本市场"],
    "计算机": [
        "人工智能", "芯片", "半导体", "数字经济", "信创", "数据安全", "算力", "大模型",
        # AI/LLM 热词
        "ChatGPT", "GPT", "生成式AI", "AIGC", "AI大模型", "LLM", "基础模型",
        "AI应用", "大模型应用", "智能体", "AI Agent", "RAG",
        "AI芯片", "GPU", "NPU", "CUDA", "AI服务器", "AI算力",
        "OpenAI", "DeepSeek", "百度文心", "通义千问", "智谱",
        "Sora", "AI视频", "多模态", "具身智能",
    ],
    "电力设备": ["新能源", "光伏", "风电", "储能", "电池", "充电桩", "碳中和"],
    "汽车": ["新能源汽车", "电动车", "智能驾驶", "汽车下乡", "自动驾驶", "智能座舱"],
    "医药生物": ["医药", "医保", "集采", "创新药", "中药", "医疗器械", "GLP-1", "减肥药"],
    "钢铁": ["钢铁", "钢材", "去产能", "限产", "粗钢"],
    "有色金属": [
        "稀土", "锂", "铜", "有色金属", "矿产",
        # 贵金属/黄金热词
        "黄金", "金价", "避险", "央行购金", "金矿",
        "白银", "银价", "铝", "锌", "镍", "锡", "钴",
    ],
    "食品饮料": ["食品安全", "白酒", "乳制品", "消费升级", "餐饮"],
    "电子": [
        "集成电路", "显示面板", "消费电子", "半导体设备", "封测",
        # AI 硬件供应链
        "先进封装", "CoWoS", "HBM", "高带宽存储", "光模块", "CPO",
        "晶圆代工", "EUV", "光刻机", "芯片制造",
    ],
    "通信": ["5G", "通信", "网络安全", "物联网", "卫星", "算力网络", "东数西算", "数据中心"],
    "传媒": ["文化", "游戏", "影视", "出版", "版权", "AI+内容", "虚拟现实", "元宇宙"],
    "公用事业": ["电力供应", "水务", "燃气", "供热", "核电"],
    "煤炭": ["煤炭", "煤矿", "煤价", "火电", "动力煤"],
    "石油石化": ["石油", "天然气", "油价", "炼化", "成品油", "OPEC", "减产"],
    "建筑材料": ["水泥", "玻璃", "建材", "地产基建"],
    "建筑装饰": ["基建", "基础设施", "城镇化", "PPP", "专项债"],
    "机械设备": ["机械", "工程机械", "机器人", "智能制造", "数控", "人形机器人", "工业机器人"],
    "国防军工": ["军工", "国防", "航空航天", "军费", "无人机", "低空经济"],
    "交通运输": ["航运", "物流", "铁路", "航空", "快递"],
    "商贸零售": ["零售", "电商", "免税", "消费券"],
    "社会服务": ["旅游", "酒店", "教育", "养老"],
    "农林牧渔": ["农业", "畜牧", "渔业", "种业", "粮食安全"],
    "家用电器": ["家电", "空调", "冰箱", "以旧换新"],
    "纺织服饰": ["纺织", "服装", "棉花"],
    "轻工制造": ["造纸", "包装", "家居"],
    "环保": ["环保", "垃圾处理", "污水处理", "碳交易", "碳排放"],
    "基础化工": ["化工", "化学品", "农药", "化肥", "涂料", "橡胶", "塑料"],
    "美容护理": ["美妆", "护肤品", "化妆品", "个人护理", "美容"],
}

# --- 情感关键词 ---
POSITIVE_KEYWORDS = [
    "支持", "鼓励", "扩大", "促进", "减税", "降费", "补贴", "利好",
    "增长", "放宽", "优化", "推动", "加快", "加大", "提升", "深化",
    "创新", "发展", "突破", "改革",
    # 市场热点正面词
    "爆发", "崛起", "火爆", "飙升", "暴涨", "新高", "革命", "颠覆",
    "井喷", "风口", "赛道", "景气", "超预期", "放量", "涨停",
]
NEGATIVE_KEYWORDS = [
    "限制", "收紧", "处罚", "风险", "下滑", "严查", "整治", "叫停",
    "约谈", "下降", "回落", "萎缩", "压降", "清退", "取缔", "禁止",
    "违规", "罚款", "监管", "严控",
    # 市场热点负面词
    "暴跌", "崩盘", "泡沫", "制裁", "封锁", "脱钩", "关税", "出口管制",
    "跌停", "爆雷", "退市", "清仓", "抛售",
]

# --- 来源层级权重 ---
TIER_WEIGHTS = {1: 1.0, 2: 0.8, 3: 0.7, 4: 0.5, 5: 0.0, 6: 0.6, 8: 0.8}  # Tier 5 暂时禁用; Tier 8 预测市场

# 已自带分析结果的来源（跳过 keyword/LLM 分析）
SKIP_ANALYSIS_SOURCES = {"polymarket"}

# ============================================================
# 美股数据配置（FMP + FRED）
# ============================================================

FMP_API_KEY = os.environ.get("FMP_API_KEY", "")
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
US_DATA_START_DATE = os.environ.get("US_DATA_START_DATE", "20150101")
FMP_RATE_LIMIT = int(os.environ.get("FMP_RATE_LIMIT", "300"))  # 免费版 300 req/min

US_INDEX_SYMBOLS = ["^GSPC", "^IXIC", "^DJI"]  # S&P 500, NASDAQ, Dow Jones

US_COMMODITY_SYMBOLS = [
    "GC=F", "SI=F", "CL=F", "BZ=F", "NG=F",  # 金银油气
    "HG=F", "ZC=F", "ZS=F", "ZW=F",           # 铜玉米大豆小麦
]

# NASDAQ 100 兜底列表（Wikipedia 不可用时使用，可在 .env 中用逗号分隔覆盖）
_raw_fallback = os.environ.get("US_FALLBACK_TICKERS", "")
US_FALLBACK_TICKERS: list[str] = [s.strip() for s in _raw_fallback.split(",") if s.strip()] if _raw_fallback.strip() else [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AMAT", "AMD", "AMGN",
    "AMZN", "ANSS", "APP", "ARM", "ASML", "AVGO", "AZN", "BIIB", "BKNG", "BKR",
    "CCEP", "CDNS", "CDW", "CEG", "CHTR", "CMCSA", "COST", "CPRT", "CRWD", "CSCO",
    "CSGP", "CTAS", "CTSH", "DASH", "DDOG", "DLTR", "DXCM", "EA", "EXC", "FANG",
    "FAST", "FTNT", "GEHC", "GFS", "GILD", "GOOG", "GOOGL", "HON", "IDXX", "INTC",
    "INTU", "ISRG", "KDP", "KHC", "KLAC", "LIN", "LRCX", "LULU", "MAR", "MCHP",
    "MDB", "MDLZ", "MELI", "META", "MNST", "MRVL", "MSFT", "MU", "NFLX", "NVDA",
    "NXPI", "ODFL", "ON", "ORLY", "PANW", "PAYX", "PCAR", "PDD", "PEP", "PYPL",
    "QCOM", "REGN", "ROP", "ROST", "SBUX", "SMCI", "SNPS", "TEAM", "TMUS", "TSLA",
    "TTD", "TTWO", "TXN", "VRSK", "VRTX", "WBD", "WDAY", "XEL", "ZS",
]

# FRED 指标映射（indicator_code → FRED series ID）
FRED_SERIES_MAP = {
    "US_GDP": "GDP",
    "US_CPI_YOY": "CPIAUCSL",
    "US_CORE_CPI": "CPILFESL",
    "US_PPI": "PPIACO",
    "US_UNEMP": "UNRATE",
    "US_NONFARM": "PAYEMS",
    "US_FED_RATE": "FEDFUNDS",
    "US_M2": "M2SL",
    "US_PMI_MFG": "MANEMP",
    "US_RETAIL": "RSAFS",
    "US_IND_PROD": "INDPRO",
    "US_HOUSING": "HOUST",
    "US_10Y": "DGS10",
    "US_2Y": "DGS2",
    "US_2Y10Y": "T10Y2Y",
    "US_TED": "TEDRATE",
    "US_VIX": "VIXCLS",
    "US_DXY": "DTWEXBGS",
    "US_INIT_CLAIMS": "ICSA",
    "US_PCE": "PCEPI",
}

# ============================================================
# Polymarket 预测市场配置（美股事件驱动预警）
# ============================================================

POLYMARKET_GAMMA_API = os.environ.get("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com")
POLYMARKET_CLOB_WS = os.environ.get("POLYMARKET_CLOB_WS", "wss://ws-subscriptions-clob.polymarket.com/ws/market")

# Spike 检测阈值（绝对价格变动）
POLYMARKET_SPIKE_5M = float(os.environ.get("POLYMARKET_SPIKE_5M", "0.05"))    # 5分钟 >5%
POLYMARKET_SPIKE_1H = float(os.environ.get("POLYMARKET_SPIKE_1H", "0.15"))    # 1小时 >15%
POLYMARKET_SPIKE_24H = float(os.environ.get("POLYMARKET_SPIKE_24H", "0.25"))  # 24小时 >25%

POLYMARKET_MIN_VOLUME = int(os.environ.get("POLYMARKET_MIN_VOLUME", "50000"))       # 最低交易量过滤
POLYMARKET_MAX_MARKETS = int(os.environ.get("POLYMARKET_MAX_MARKETS", "50"))        # 最大监控市场数
POLYMARKET_SNAPSHOT_INTERVAL = int(os.environ.get("POLYMARKET_SNAPSHOT_INTERVAL", "60"))      # 快照间隔（秒）
POLYMARKET_DISCOVERY_INTERVAL = int(os.environ.get("POLYMARKET_DISCOVERY_INTERVAL", "3600"))  # 市场发现间隔（秒）
POLYMARKET_LLM_COOLDOWN = int(os.environ.get("POLYMARKET_LLM_COOLDOWN", "300"))               # 同一事件 LLM 分析冷却（秒）

# ============================================================
# 日志配置
# ============================================================

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
