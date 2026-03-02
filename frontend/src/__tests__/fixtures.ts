/**
 * Mock API response fixtures — mirrors actual backend JSON shapes.
 * Used for both frontend unit tests and frontend-backend integration verification.
 */

// GET /api/data/status
export const dataStatusResponse = {
  tables: [
    { table: 'stock_basic', label: '股票基本信息', count: 5342 },
    { table: 'daily_price', label: '日线行情', count: 12850000, latest_date: '2025-02-26' },
    { table: 'financial_data', label: '财务数据', count: 98000 },
    { table: 'industry_class', label: '行业分类', count: 5200 },
    { table: 'paper_account', label: '模拟盘账户', count: 1 },
    { table: 'paper_position', label: '模拟盘持仓', count: 10 },
    { table: 'paper_transaction', label: '模拟盘交易', count: 350 },
    { table: 'paper_nav', label: '模拟盘净值', count: 200 },
    { table: 'commodity_price', label: '商品期货价格', count: 45000 },
    { table: 'macro_indicator', label: '宏观经济指标', count: 3200 },
    { table: 'industry_factor_config', label: '行业因子配置', count: 180 },
    { table: 'policy_article', label: '政策文章', count: 1500 },
    { table: 'scrape_log', label: '抓取日志', count: 80 },
  ],
  latest_trade_date: '2025-02-26',
}

// POST /api/data/download  response
export const taskSubmitResponse = {
  task_id: 'abc12345',
  name: '全量下载',
}

// GET /api/tasks/
export const taskListResponse = [
  {
    task_id: 'abc12345',
    name: '全量下载',
    status: 'running',
    progress: 45,
    message: '下载日线行情...',
    result: null,
    error: '',
    created_at: 1740000000,
    started_at: 1740000001,
    finished_at: null,
    elapsed: 120.5,
  },
  {
    task_id: 'def67890',
    name: '回测 2020-01-01~2024-12-31',
    status: 'completed',
    progress: 100,
    message: '完成',
    result: { summary: { '总收益率': '125.3%' } },
    error: '',
    created_at: 1739999000,
    started_at: 1739999001,
    finished_at: 1739999300,
    elapsed: 299.0,
  },
]

// GET /api/tasks/:id  (completed task with result)
export const taskCompletedResponse = {
  task_id: 'def67890',
  name: '回测 2020-01-01~2024-12-31',
  status: 'completed',
  progress: 100,
  message: '完成',
  result: {
    summary: {
      '总收益率': '125.3%',
      '年化收益率': '18.5%',
      '最大回撤': '-15.2%',
      '夏普比率': '1.32',
      '胜率': '58.3%',
    },
    nav: [
      { date: '2020-01-02', nav: 1.0 },
      { date: '2020-06-30', nav: 1.12 },
      { date: '2021-01-04', nav: 1.35 },
      { date: '2021-06-30', nav: 1.48 },
      { date: '2022-01-04', nav: 1.52 },
      { date: '2022-06-30', nav: 1.38 },
      { date: '2023-01-03', nav: 1.65 },
      { date: '2023-06-30', nav: 1.82 },
      { date: '2024-01-02', nav: 1.95 },
      { date: '2024-12-31', nav: 2.253 },
    ],
    benchmark: [
      { date: '2020-01-02', nav: 1.0 },
      { date: '2020-06-30', nav: 1.05 },
      { date: '2021-01-04', nav: 1.15 },
      { date: '2021-06-30', nav: 1.1 },
      { date: '2022-01-04', nav: 1.08 },
      { date: '2022-06-30', nav: 0.95 },
      { date: '2023-01-03', nav: 1.02 },
      { date: '2023-06-30', nav: 1.08 },
      { date: '2024-01-02', nav: 1.12 },
      { date: '2024-12-31', nav: 1.18 },
    ],
    trades: [
      { date: '2020-01-03', ts_code: '000001.SZ', name: '平安银行', direction: 'BUY', price: 16.5, volume: 1000, amount: 16500 },
      { date: '2020-01-03', ts_code: '600036.SH', name: '招商银行', direction: 'BUY', price: 38.2, volume: 500, amount: 19100 },
      { date: '2020-02-03', ts_code: '000001.SZ', name: '平安银行', direction: 'SELL', price: 15.8, volume: 1000, amount: 15800 },
    ],
    monthly: [
      { year: 2020, month: 1, return: 0.025 },
      { year: 2020, month: 2, return: -0.012 },
      { year: 2020, month: 3, return: 0.038 },
    ],
    drawdown: [
      { date: '2020-01-02', drawdown: 0 },
      { date: '2020-06-30', drawdown: -0.02 },
      { date: '2022-06-30', drawdown: -0.152 },
      { date: '2024-12-31', drawdown: -0.01 },
    ],
  },
  error: '',
  created_at: 1739999000,
  started_at: 1739999001,
  finished_at: 1739999300,
  elapsed: 299.0,
}

// GET /api/universe
export const universeResponse = {
  date: '2025-02-26',
  total: 4200,
  limit_up: 35,
  limit_down: 12,
  industry_distribution: {
    '电子': 380,
    '医药生物': 350,
    '计算机': 280,
    '机械设备': 260,
    '化工': 250,
    '电力设备': 230,
    '汽车': 180,
    '有色金属': 150,
    '食品饮料': 120,
    '银行': 42,
  },
  stocks: [
    { ts_code: '000001.SZ', name: '平安银行', industry_name: '银行', close: 12.35, amount: 985000, pct_chg: 1.23, is_limit_up: false, is_limit_down: false },
    { ts_code: '000002.SZ', name: '万科A', industry_name: '房地产', close: 8.92, amount: 1250000, pct_chg: -0.56, is_limit_up: false, is_limit_down: false },
    { ts_code: '600036.SH', name: '招商银行', industry_name: '银行', close: 35.8, amount: 2100000, pct_chg: 0.85, is_limit_up: false, is_limit_down: false },
    { ts_code: '600519.SH', name: '贵州茅台', industry_name: '食品饮料', close: 1520.0, amount: 5800000, pct_chg: -0.32, is_limit_up: false, is_limit_down: false },
    { ts_code: '300750.SZ', name: '宁德时代', industry_name: '电力设备', close: 210.5, amount: 4200000, pct_chg: 2.15, is_limit_up: false, is_limit_down: false },
  ],
}

// GET /api/select
export const selectResponse = {
  date: '2025-02-26',
  total: 4200,
  top_stocks: [
    { ts_code: '002415.SZ', name: '海康威视', industry_name: '计算机', score: 2.856, weight: 0.10 },
    { ts_code: '000858.SZ', name: '五粮液', industry_name: '食品饮料', score: 2.734, weight: 0.10 },
    { ts_code: '600036.SH', name: '招商银行', industry_name: '银行', score: 2.651, weight: 0.10 },
    { ts_code: '601318.SH', name: '中国平安', industry_name: '非银金融', score: 2.580, weight: 0.10 },
    { ts_code: '000333.SZ', name: '美的集团', industry_name: '家用电器', score: 2.512, weight: 0.10 },
  ],
  by_industry: {
    '银行': [
      { ts_code: '600036.SH', name: '招商银行', score: 2.651 },
      { ts_code: '601166.SH', name: '兴业银行', score: 2.320 },
    ],
    '食品饮料': [
      { ts_code: '000858.SZ', name: '五粮液', score: 2.734 },
      { ts_code: '600519.SH', name: '贵州茅台', score: 2.210 },
    ],
  },
}

// GET /api/factors
export const factorDetailResponse = {
  ts_code: '600036.SH',
  score: 2.651,
  EP: 0.85,
  BP: 0.62,
  ROE_TTM: 1.23,
  GROSS_MARGIN: 0.45,
  PROFIT_STB: 0.92,
  MARGIN_TREND: 0.33,
  NET_PROFIT_YOY: 0.78,
  REVENUE_YOY: 0.56,
  MOM_1M: -0.12,
  MOM_3M: 0.45,
  MOM_12M: 1.05,
  REV_5D: -0.23,
  IND_MOM: 0.67,
  RESIDUAL_MOM: 0.34,
  TURN_20D: -0.56,
  VOL_20D: -0.78,
  PRICE_DEV_60D: 0.12,
  SIZE: -1.23,
  VOL_PRICE_DIV: 0.45,
}

// GET /api/paper/account
export const paperAccountResponse = {
  account_name: 'default',
  initial_capital: 1000000,
  cash: 450000,
  total_assets: 1125000,
  pnl: 125000,
  pnl_pct: 0.125,
}

// GET /api/paper/positions
export const paperPositionsResponse = [
  { ts_code: '600036.SH', name: '招商银行', volume: 2000, cost_basis: 34.5, current_price: 35.8, market_value: 71600, pnl: 2600, pnl_pct: 0.0377 },
  { ts_code: '000858.SZ', name: '五粮液', volume: 500, cost_basis: 150.2, current_price: 158.0, market_value: 79000, pnl: 3900, pnl_pct: 0.052 },
]

// GET /api/paper/nav
export const paperNavResponse = [
  { date: '2025-02-01', nav: 1.08 },
  { date: '2025-02-10', nav: 1.10 },
  { date: '2025-02-20', nav: 1.12 },
  { date: '2025-02-26', nav: 1.125 },
]

// GET /api/paper/transactions
export const paperTransactionsResponse = [
  { trade_date: '2025-02-26', ts_code: '600036.SH', name: '招商银行', direction: 'BUY', price: 35.8, filled_volume: 500, amount: 17900, reason: '调仓信号' },
  { trade_date: '2025-02-25', ts_code: '000001.SZ', name: '平安银行', direction: 'SELL', price: 12.3, filled_volume: 1000, amount: 12300, reason: '止盈' },
]

// GET /api/sentiment/status
export const sentimentStatusResponse = {
  sources: [
    { source: 'gov_cn', tier: 1, tier_name: '最高层', count: 320, earliest: '2024-01-15', latest: '2025-02-25' },
    { source: 'csrc', tier: 3, tier_name: '金融监管', count: 180, earliest: '2024-03-10', latest: '2025-02-24' },
    { source: 'pbc', tier: 3, tier_name: '金融监管', count: 150, earliest: '2024-02-20', latest: '2025-02-25' },
    { source: 'twitter_trump', tier: 5, tier_name: '美国政策', count: 40, earliest: '2025-01-20', latest: '2025-02-26' },
  ],
  total: 690,
}

// GET /api/sentiment/articles
export const sentimentArticlesResponse = {
  articles: [
    { source: 'csrc', tier: 3, title: '证监会发布关于加强上市公司监管的通知', url: 'https://www.csrc.gov.cn/example1', publish_date: '2025-02-25', category: '监管动态', summary: '为进一步加强上市公司监管，规范信息披露行为...', scraped_at: '2025-02-25 10:30:00' },
    { source: 'pbc', tier: 3, title: '中国人民银行公开市场业务交易公告', url: 'https://www.pbc.gov.cn/example2', publish_date: '2025-02-24', category: '公开市场', summary: '2025年2月24日，中国人民银行以利率招标方式开展了...', scraped_at: '2025-02-24 09:15:00' },
  ],
  total: 690,
  page: 1,
  page_size: 20,
}

// GET /api/config/settings
export const settingsResponse = {
  MAX_HOLDINGS: 10,
  MIN_HOLDINGS: 0,
  MIN_SELECT_SCORE: 0,
  MAX_SINGLE_WEIGHT: 0.05,
  MAX_INDUSTRY_WEIGHT: 0.3,
  BUY_COMMISSION: 0.00075,
  SELL_COMMISSION: 0.00075,
  STAMP_TAX: 0.001,
  SLIPPAGE: 0.001,
  MAX_DRAWDOWN_THRESHOLD: 0.25,
  DRAWDOWN_REDUCE_POSITION: 0.7,
  MIN_DAILY_TURNOVER: 50000000,
  IPO_FILTER_DAYS: 180,
  NEUTRALIZE_MODE: 'full',
  NONLINEAR_SIZE: 0,
  USE_VOL_TARGETING: 0,
  TARGET_VOL: 0.2,
  VOL_LOOKBACK_DAYS: 20,
  VOL_SCALE_MIN: 0.3,
  VOL_SCALE_MAX: 1.0,
  PAPER_INITIAL_CAPITAL: 1000000,
  PAPER_ACCOUNT_NAME: 'default',
  TRADER_TYPE: 'paper',
  DATA_START_DATE: '20150101',
  EXCLUDE_STAR_MARKET: 1,
  LOG_LEVEL: 'INFO',
  _sensitive: {
    TUSHARE_TOKEN: '***已配置***',
    TWITTER_USERNAME: '',
    TWITTER_EMAIL: '',
    TWITTER_PASSWORD: '',
    MYSQL_HOST: '***已配置***',
    MYSQL_PORT: '***已配置***',
    MYSQL_USER: '***已配置***',
    MYSQL_PASSWORD: '***已配置***',
    MYSQL_DATABASE: '***已配置***',
  },
}

// GET /api/config/industry-factors
export const industryFactorsResponse = {
  industries: {
    '银行': {
      EP: { weight: 1.5, description: '银行股估值因子加权' },
      ROE_TTM: { weight: 1.2, description: '' },
    },
    '电子': {
      MOM_3M: { weight: 1.3, description: '电子行业动量加权' },
      REVENUE_YOY: { weight: 1.5, description: '' },
    },
  },
}
