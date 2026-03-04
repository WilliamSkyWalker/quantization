<script setup lang="ts">
import { h, ref, onMounted, computed } from 'vue'
import { useMessage, NTag, NIcon, NButton, NSpace, NDivider, NDataTable as NDataTableComp } from 'naive-ui'
import {
  SettingsOutline, DownloadOutline, RefreshOutline,
  SearchOutline, OpenOutline, EyeOutline,
} from '@vicons/ionicons5'
import type { DataTableColumns } from 'naive-ui'
import {
  getDataStatus, startDownload, startUpdate, startBackfillIncome, initDatabase,
  startSentimentDownloadAndAnalyze, startSentimentBackfillAnalyze, startSentimentBackfillContent,
  startSentimentBackfillLLM, getSentimentStatus, getSentimentArticles, getSentimentAnalysisStats,
  startSentimentDownload, browseData,
} from '../api'
import { backtestDiscover, backtestDownload, getBacktestMarkets } from '../api/polymarket'
import { formatDate } from '../utils/format'
import { useTaskStore } from '../stores/task'

const message = useMessage()
const taskStore = useTaskStore()
const loading = ref(false)
const running = ref(false)
const tables = ref<any[]>([])

// ============================================================
// Active main tab
// ============================================================
const mainTab = ref('operations')

// ============================================================
// Data operations table
// ============================================================
interface DataTypeRow {
  key: string
  label: string
  browseTable?: string   // table name for data browser
  downloadAction?: string
  downloadLabel?: string
  updateAction?: string
  updateLabel?: string
  backfillActions?: { action: string; label: string; handler: () => void }[]
}

const dataTypes: DataTypeRow[] = [
  // --- A股数据 ---
  { key: 'a_divider', label: '── A股数据 ──' },
  {
    key: 'list', label: '股票列表', browseTable: 'stock_basic',
    downloadAction: 'download_list', downloadLabel: '下载',
    updateAction: 'update_list', updateLabel: '刷新',
  },
  {
    key: 'daily', label: '日线行情', browseTable: 'daily_price',
    downloadAction: 'download_daily', downloadLabel: '下载',
    updateAction: 'update_daily', updateLabel: '更新',
    backfillActions: [{ action: 'backfill_daily', label: '补录缺失', handler: () => runAction('backfill_daily', '补录日线行情') }],
  },
  {
    key: 'financial', label: '财务数据', browseTable: 'financial_data',
    downloadAction: 'download_financial', downloadLabel: '下载',
    updateAction: 'update_financial', updateLabel: '更新',
    backfillActions: [
      { action: 'backfill_income', label: '回填利润', handler: () => runBackfill() },
      { action: 'backfill_financial', label: '补录季度', handler: () => runAction('backfill_financial', '补录财务季度') },
    ],
  },
  {
    key: 'valuation', label: '估值快照', browseTable: 'daily_price',
    downloadAction: 'download_valuation', downloadLabel: '下载',
    updateAction: 'update_valuation', updateLabel: '刷新',
  },
  {
    key: 'industry', label: '行业分类', browseTable: 'industry_class',
    downloadAction: 'download_industry', downloadLabel: '下载',
    updateAction: 'update_industry', updateLabel: '刷新',
  },
  {
    key: 'index', label: '指数数据',
    downloadAction: 'download_index', downloadLabel: '下载',
    updateAction: 'update_index', updateLabel: '更新',
    backfillActions: [{ action: 'backfill_index', label: '补录缺失', handler: () => runAction('backfill_index', '补录指数数据') }],
  },
  {
    key: 'commodity', label: '商品期货', browseTable: 'commodity_price',
    downloadAction: 'download_commodity', downloadLabel: '下载',
    updateAction: 'update_commodity', updateLabel: '更新',
    backfillActions: [{ action: 'backfill_commodity', label: '补录缺失', handler: () => runAction('backfill_commodity', '补录商品期货') }],
  },
  {
    key: 'macro', label: '宏观数据', browseTable: 'macro_indicator',
    downloadAction: 'download_macro', downloadLabel: '下载',
    updateAction: 'update_macro', updateLabel: '更新',
    backfillActions: [{ action: 'backfill_macro', label: '补录全量', handler: () => runAction('backfill_macro', '补录宏观数据') }],
  },
  {
    key: 'reports', label: '券商研报', browseTable: 'research_report',
    downloadAction: 'download_reports', downloadLabel: '下载',
    updateAction: 'update_reports', updateLabel: '刷新',
    backfillActions: [{ action: 'backfill_reports', label: '强制全量', handler: () => runAction('backfill_reports', '补录券商研报') }],
  },
  {
    key: 'sentiment', label: '舆情数据',
    downloadAction: 'sentiment_download_analyze', downloadLabel: '抓取+分析',
    updateAction: 'update_sentiment', updateLabel: '更新',
    backfillActions: [
      { action: 'backfill_analyze', label: '补分析', handler: () => runBackfillAnalyze() },
      { action: 'backfill_content', label: '补全文', handler: () => runBackfillContent() },
      { action: 'backfill_llm', label: '补LLM', handler: () => runBackfillLLM() },
    ],
  },
  // --- 美股数据 ---
  { key: 'us_divider', label: '── 美股数据 ──' },
  {
    key: 'us_list', label: '🇺🇸 股票列表', browseTable: 'us_stock_basic',
    downloadAction: 'download_us_list', downloadLabel: '下载',
  },
  {
    key: 'us_daily', label: '🇺🇸 日线行情', browseTable: 'us_daily_price',
    downloadAction: 'download_us_daily', downloadLabel: '下载',
    updateAction: 'update_us_daily', updateLabel: '更新',
  },
  {
    key: 'us_financial', label: '🇺🇸 财务数据', browseTable: 'us_financial_data',
    downloadAction: 'download_us_financial', downloadLabel: '下载',
    updateAction: 'update_us_financial', updateLabel: '更新',
  },
  {
    key: 'us_industry', label: '🇺🇸 行业分类', browseTable: 'us_industry_class',
    downloadAction: 'download_us_industry', downloadLabel: '下载',
  },
  {
    key: 'us_index', label: '🇺🇸 指数数据', browseTable: 'us_index_daily',
    downloadAction: 'download_us_index', downloadLabel: '下载',
    updateAction: 'update_us_index', updateLabel: '更新',
  },
  {
    key: 'us_macro', label: '🇺🇸 宏观数据', browseTable: 'us_macro_indicator',
    downloadAction: 'download_us_macro', downloadLabel: '下载',
    updateAction: 'update_us_macro', updateLabel: '更新',
  },
  {
    key: 'us_commodity', label: '🇺🇸 商品期货', browseTable: 'us_commodity_price',
    downloadAction: 'download_us_commodity', downloadLabel: '下载',
    updateAction: 'update_us_commodity', updateLabel: '更新',
  },
  {
    key: 'us_analyst', label: '🇺🇸 分析师评级', browseTable: 'us_analyst_recommendation',
    downloadAction: 'download_us_analyst', downloadLabel: '下载',
    updateAction: 'update_us_analyst', updateLabel: '更新',
  },
  {
    key: 'us_sec', label: '🇺🇸 SEC公告', browseTable: 'us_sec_filing',
    downloadAction: 'download_us_sec_filing', downloadLabel: '下载',
    updateAction: 'update_us_sec_filing', updateLabel: '更新',
  },
  {
    key: 'us_corp', label: '🇺🇸 公司行动', browseTable: 'us_corporate_action',
    downloadAction: 'download_us_corporate_action', downloadLabel: '下载',
    updateAction: 'update_us_corporate_action', updateLabel: '更新',
  },
  // --- Polymarket ---
  { key: 'pm_divider', label: '── Polymarket ──' },
  {
    key: 'pm_markets', label: '已结算市场', browseTable: 'polymarket_event',
    downloadAction: 'pm_discover', downloadLabel: '发现市场',
    updateAction: 'pm_download', updateLabel: '下载数据',
  },
]

const statusColumns: DataTableColumns = [
  { title: '表名', key: 'label', width: 150 },
  { title: '表', key: 'table', width: 180 },
  {
    title: '行数', key: 'count', width: 120, align: 'right',
    render: (row: any) => row.count?.toLocaleString(),
  },
  { title: '更新日期', key: 'latest_date', width: 160 },
  {
    title: '状态', key: 'error', width: 80,
    render: (row: any) => {
      if (row.error) return h(NTag, { type: 'warning', size: 'small' }, { default: () => row.error })
      if (row.count > 0) return h(NTag, { type: 'success', size: 'small' }, { default: () => '正常' })
      return h(NTag, { size: 'small' }, { default: () => '空' })
    },
  },
  {
    title: '', key: 'browse', width: 70,
    render: (row: any) => {
      const browsable = Object.values(_BROWSE_MAP).includes(row.table)
      if (!browsable) return null
      return h(NButton, {
        size: 'tiny', text: true, type: 'info',
        onClick: () => openBrowser(row.table),
      }, {
        default: () => [h(NIcon, { size: 14, style: 'margin-right:2px' }, { default: () => h(EyeOutline) }), '查看'],
      })
    },
  },
]

// Map dataType key → table name for row count lookup
const _COUNT_TABLE_MAP: Record<string, string> = {
  list: 'stock_basic',
  daily: 'daily_price',
  financial: 'financial_data',
  valuation: 'financial_data',
  industry: 'industry_class',
  index: 'daily_price',
  commodity: 'commodity_price',
  macro: 'macro_indicator',
  reports: 'research_report',
  sentiment: 'policy_article',
  // 美股
  us_list: 'us_stock_basic',
  us_daily: 'us_daily_price',
  us_financial: 'us_financial_data',
  us_industry: 'us_industry_class',
  us_index: 'us_index_daily',
  us_macro: 'us_macro_indicator',
  us_commodity: 'us_commodity_price',
  us_analyst: 'us_analyst_recommendation',
  us_sec: 'us_sec_filing',
  us_corp: 'us_corporate_action',
  // Polymarket
  pm_markets: 'polymarket_event',
}

const tableInfoMap = computed(() => {
  const m: Record<string, { count: number; dataDate?: string; latestDate?: string }> = {}
  for (const t of tables.value) {
    m[t.table] = { count: t.count ?? 0, dataDate: t.data_date, latestDate: t.latest_date }
  }
  return m
})

// Mapping for browsable tables
const _BROWSE_MAP: Record<string, string> = {}
dataTypes.forEach(d => { if (d.browseTable) _BROWSE_MAP[d.key] = d.browseTable })
// Also add non-dataType tables
const _ALL_BROWSE = [
  'stock_basic', 'daily_price', 'financial_data', 'industry_class',
  'commodity_price', 'macro_indicator', 'policy_article', 'policy_analysis',
  'research_report', 'paper_account', 'paper_position', 'paper_transaction', 'paper_nav',
  // 美股
  'us_stock_basic', 'us_daily_price', 'us_financial_data', 'us_industry_class',
  'us_index_daily', 'us_macro_indicator', 'us_commodity_price',
  'us_analyst_recommendation', 'us_sec_filing', 'us_corporate_action',
  // Polymarket
  'polymarket_event', 'polymarket_price_snapshot', 'polymarket_alert',
]

const opColumns: DataTableColumns = [
  {
    type: 'expand',
    expandable: (row: any) => row.key === 'sentiment',
    renderExpand: () => h('div', { style: 'padding: 4px 0' }, [
      h('div', {
        style: 'display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px',
      }, [
        h(NSpace, { size: 'small', align: 'center' }, {
          default: () => [
            h('span', { style: 'color: #606266; font-weight: 600; font-size: 13px' },
              `共 ${sentTotal.value} 篇`),
            h(NTag, { type: 'info', size: 'small' },
              { default: () => `关键词已分析: ${analysisStats.value.keyword_analyzed}` }),
            h(NTag, { type: 'success', size: 'small' },
              { default: () => `LLM已分析: ${analysisStats.value.llm_analyzed}` }),
            h(NTag, { type: 'warning', size: 'small' },
              { default: () => `待分析: ${analysisStats.value.pending_keyword}` }),
            h(NTag, {
              type: analysisStats.value.llm_available ? 'success' : 'default',
              size: 'small',
            }, { default: () => `LLM: ${analysisStats.value.llm_available ? '可用' : '未配置'}` }),
          ],
        }),
        h(NButton, {
          size: 'tiny',
          onClick: () => loadSentimentStatus(),
          loading: sentimentLoading.value,
        }, {
          icon: () => h(NIcon, null, { default: () => h(RefreshOutline) }),
          default: () => '刷新',
        }),
      ]),
      h(NDataTableComp, {
        columns: sourceColumns,
        data: sortedSources.value,
        loading: sentimentLoading.value,
        rowKey: (row: any) => row.source,
        striped: true,
        size: 'small',
      }),
    ]),
  },
  {
    title: '数据类型', key: 'label', width: 100,
    render: (row: any) => {
      if (row.key.endsWith('_divider')) {
        return h('span', { style: 'font-weight: 700; color: #409eff; font-size: 12px' }, row.label)
      }
      return row.label
    },
  },
  {
    title: '数据总量', key: 'count', width: 90, align: 'right',
    render: (row: any) => {
      if (row.key.endsWith('_divider')) return null
      const tableName = _COUNT_TABLE_MAP[row.key]
      if (!tableName) return '-'
      const info = tableInfoMap.value[tableName]
      if (!info) return '-'
      return h('span', {
        style: info.count > 0 ? 'font-weight: 600; color: #18a058' : 'color: #c0c4cc',
      }, info.count.toLocaleString())
    },
  },
  {
    title: '数据日期', key: 'dataDate', width: 100,
    render: (row: any) => {
      if (row.key.endsWith('_divider')) return null
      const tableName = _COUNT_TABLE_MAP[row.key]
      if (!tableName) return '-'
      const info = tableInfoMap.value[tableName]
      return h('span', { style: 'font-size: 12px; color: #606266' }, info?.dataDate || '-')
    },
  },
  {
    title: '更新时间', key: 'latestDate', width: 150,
    render: (row: any) => {
      if (row.key.endsWith('_divider')) return null
      const tableName = _COUNT_TABLE_MAP[row.key]
      if (!tableName) return '-'
      const info = tableInfoMap.value[tableName]
      return h('span', { style: 'font-size: 12px; color: #909399' }, info?.latestDate || '-')
    },
  },
  {
    title: '全量下载', key: 'download', width: 110, align: 'center',
    render: (row: any) => {
      if (!row.downloadAction) return null
      if (row.key === 'sentiment') {
        return h(NButton, {
          size: 'tiny', type: 'success',
          onClick: () => runSentimentDownloadAndAnalyze(),
        }, { default: () => row.downloadLabel })
      }
      if (row.key === 'pm_markets') {
        return h(NButton, {
          size: 'tiny', type: 'success',
          onClick: () => runPmDiscover(),
        }, { default: () => row.downloadLabel })
      }
      return h(NButton, {
        size: 'tiny', type: 'success',
        onClick: () => runAction(row.downloadAction, row.downloadLabel + row.label),
      }, { default: () => row.downloadLabel })
    },
  },
  {
    title: '增量更新', key: 'update', width: 110, align: 'center',
    render: (row: any) => {
      if (!row.updateAction) return null
      if (row.key === 'pm_markets') {
        return h(NButton, {
          size: 'tiny', type: 'warning',
          onClick: () => runPmDownload(),
        }, { default: () => row.updateLabel })
      }
      return h(NButton, {
        size: 'tiny', type: 'warning',
        onClick: () => runAction(row.updateAction, row.updateLabel + row.label),
      }, { default: () => row.updateLabel })
    },
  },
  {
    title: '补录', key: 'backfill', width: 140,
    render: (row: any) => {
      if (!row.backfillActions || row.backfillActions.length === 0) return null
      return h(NSpace, { size: 'small' }, {
        default: () => row.backfillActions.map((a: any) =>
          h(NButton, { size: 'tiny', onClick: a.handler }, { default: () => a.label })
        ),
      })
    },
  },
  {
    title: '查看', key: 'view', width: 70, align: 'center',
    render: (row: any) => {
      if (row.key === 'sentiment') {
        return h(NButton, {
          size: 'tiny', type: 'info',
          onClick: () => { mainTab.value = 'articles'; loadArticles(1) },
        }, { default: () => '查看' })
      }
      if (!row.browseTable) return null
      return h(NButton, {
        size: 'tiny', type: 'info',
        onClick: () => openBrowser(row.browseTable),
      }, { default: () => '查看' })
    },
  },
]

// ============================================================
// Data operations actions
// ============================================================
async function loadStatus() {
  loading.value = true
  try {
    const { data } = await getDataStatus()
    tables.value = data.tables
  } finally {
    loading.value = false
  }
}

async function runAction(action: string, name: string) {
  running.value = true
  try {
    const { data } = await startDownload(action)
    taskStore.trackTask(data.task_id, name)
    message.success(`${name} 任务已启动`)
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    running.value = false
  }
}

async function runUpdate() {
  running.value = true
  try {
    const { data } = await startUpdate()
    taskStore.trackTask(data.task_id, '一键增量更新')
    message.success('一键增量更新任务已启动')
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    running.value = false
  }
}

async function runFullDownload() {
  running.value = true
  try {
    const { data: d1 } = await startDownload('download_all')
    taskStore.trackTask(d1.task_id, '全量下载(行情)')
    const { data: d2 } = await startDownload('download_extra')
    taskStore.trackTask(d2.task_id, '全量下载(财务)')
    const { data: d3 } = await startDownload('download_commodity')
    taskStore.trackTask(d3.task_id, '全量下载(商品)')
    const { data: d4 } = await startDownload('download_macro')
    taskStore.trackTask(d4.task_id, '全量下载(宏观)')
    const { data: d5 } = await startDownload('download_index')
    taskStore.trackTask(d5.task_id, '全量下载(指数)')
    const { data: d6 } = await startDownload('download_reports')
    taskStore.trackTask(d6.task_id, '全量下载(研报)')
    message.success('一键全量下载: 6 个任务已启动')
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    running.value = false
  }
}

async function runUSFullDownload() {
  running.value = true
  try {
    const { data } = await startDownload('download_us_all')
    taskStore.trackTask(data.task_id, '美股全量下载')
    message.success('美股全量下载任务已启动')
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    running.value = false
  }
}

async function runUSUpdate() {
  running.value = true
  try {
    const actions = [
      { action: 'update_us_daily', name: '美股日线' },
      { action: 'update_us_financial', name: '美股财务' },
      { action: 'update_us_index', name: '美股指数' },
      { action: 'update_us_macro', name: '美股宏观' },
      { action: 'update_us_commodity', name: '美股商品' },
      { action: 'update_us_analyst', name: '美股评级' },
      { action: 'update_us_sec_filing', name: '美股SEC' },
      { action: 'update_us_corporate_action', name: '美股公司行动' },
    ]
    for (const a of actions) {
      const { data } = await startDownload(a.action)
      taskStore.trackTask(data.task_id, a.name)
    }
    message.success(`美股增量更新: ${actions.length} 个任务已启动`)
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    running.value = false
  }
}

async function runBackfill() {
  running.value = true
  try {
    const { data } = await startBackfillIncome()
    taskStore.trackTask(data.task_id, '回填利润表')
    message.success('回填利润表任务已启动')
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    running.value = false
  }
}

async function runSentimentDownloadAndAnalyze() {
  running.value = true
  try {
    const { data } = await startSentimentDownloadAndAnalyze()
    taskStore.trackTask(data.task_id, '抓取+分析舆情')
    message.success('舆情抓取+分析任务已启动')
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    running.value = false
  }
}

async function runBackfillAnalyze() {
  running.value = true
  try {
    const { data } = await startSentimentBackfillAnalyze()
    taskStore.trackTask(data.task_id, '补录舆情分析')
    message.success('补录舆情分析任务已启动')
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    running.value = false
  }
}

async function runBackfillContent() {
  running.value = true
  try {
    const { data } = await startSentimentBackfillContent()
    taskStore.trackTask(data.task_id, '补录全文')
    message.success('补录全文任务已启动')
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    running.value = false
  }
}

async function runBackfillLLM() {
  running.value = true
  try {
    const { data } = await startSentimentBackfillLLM()
    taskStore.trackTask(data.task_id, '补录LLM打分')
    message.success('补录LLM打分任务已启动')
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    running.value = false
  }
}

// ============================================================
// Polymarket data prep
// ============================================================
async function runPmDiscover() {
  running.value = true
  try {
    const { data } = await backtestDiscover({ limit: 50 })
    taskStore.trackTask(data.task_id, '发现已结算市场')
    message.success('Polymarket 市场发现任务已启动')
  } catch (e: any) {
    message.error(e.response?.data?.error || '发现市场失败')
  } finally {
    running.value = false
  }
}

async function runPmDownload() {
  running.value = true
  try {
    const { data } = await backtestDownload({ limit: 20, fidelity: 60 })
    taskStore.trackTask(data.task_id, '下载 Polymarket 历史数据')
    message.success('Polymarket 历史数据下载任务已启动')
  } catch (e: any) {
    message.error(e.response?.data?.error || '下载历史数据失败')
  } finally {
    running.value = false
  }
}

async function runInit() {
  running.value = true
  try {
    await initDatabase()
    message.success('数据库表结构已初始化')
    await loadStatus()
  } catch (e: any) {
    message.error('初始化失败')
  } finally {
    running.value = false
  }
}

// ============================================================
// Sentiment monitoring (merged from Sentiment.vue)
// ============================================================
const sentimentLoading = ref(false)
const scraping = ref(false)
const sentSources = ref<any[]>([])
const sentTotal = ref(0)
const analysisStats = ref({ keyword_analyzed: 0, llm_analyzed: 0, pending_keyword: 0, llm_available: false })

// Articles
const articles = ref<any[]>([])
const articleTotal = ref(0)
const articlePage = ref(1)
const articleLoading = ref(false)
const currentSource = ref('')
const currentCategory = ref<string | null>(null)
const dateRange = ref<[number, number] | null>(null)
const keyword = ref('')
const categories = ref<string[]>([])

// Drawer
const drawerVisible = ref(false)
const selectedArticle = ref<any>(null)

const tierNames: Record<number, string> = {
  1: '最高层', 2: '产业层', 3: '金融监管', 4: '专项行业', 5: '美国政策', 6: '财经媒体', 7: '券商研报', 8: '预测市场',
}
const tierColors: Record<number, string> = {
  1: '#f56c6c', 2: '#e6a23c', 3: '#409eff', 4: '#67c23a', 5: '#909399', 6: '#f59e0b', 7: '#8b5cf6', 8: '#3b82f6',
}

const sourceColumns: DataTableColumns = [
  {
    title: '层级', key: 'tier', width: 110,
    render: (row: any) => h(NTag, {
      color: { color: tierColors[row.tier], textColor: '#fff' },
      size: 'small',
    }, { default: () => `T${row.tier} ${tierNames[row.tier] || ''}` }),
  },
  { title: '来源', key: 'label', width: 130 },
  {
    title: '篇数', key: 'count', width: 80, align: 'right',
    render: (row: any) => h('span', {
      style: row.count > 0 ? 'font-weight: 600' : 'color: #c0c4cc',
    }, row.count?.toLocaleString() ?? '0'),
  },
  { title: '最早', key: 'earliest', width: 110, render: (row: any) => row.earliest || '-' },
  { title: '最新', key: 'latest', width: 110, render: (row: any) => row.latest || '-' },
  {
    title: '操作', key: 'actions', width: 340,
    render: (row: any) => h(NSpace, { size: 'small' }, {
      default: () => [
        h(NButton, {
          size: 'tiny', type: 'primary', disabled: scraping.value,
          onClick: () => scrapeSource(row.source),
        }, { default: () => '抓取' }),
        h(NButton, {
          size: 'tiny', type: 'warning', disabled: scraping.value,
          onClick: () => scrapeSourceIncremental(row.source),
        }, { default: () => '增量' }),
        h(NButton, {
          size: 'tiny', disabled: scraping.value,
          onClick: () => scrapeSourceBackfill(row.source),
        }, { default: () => '补录' }),
        h(NButton, {
          size: 'tiny', disabled: row.count === 0,
          onClick: () => filterBySource(row.source),
        }, { default: () => '查看' }),
      ],
    }),
  },
]

const sortedSources = computed(() =>
  [...sentSources.value].sort((a, b) => a.tier - b.tier || a.source.localeCompare(b.source))
)

const articleColumns: DataTableColumns = [
  {
    title: '来源', key: 'source', width: 120,
    render: (row: any) => h(NTag, {
      type: row.tier <= 2 ? 'error' : row.tier <= 3 ? 'warning' : 'default', size: 'small',
    }, { default: () => row.source }),
  },
  {
    title: '栏目', key: 'category', width: 100,
    ellipsis: { tooltip: true },
    render: (row: any) => h('span', { style: 'color: #909399; font-size: 12px' }, row.category || '-'),
  },
  {
    title: '标题', key: 'title', minWidth: 300,
    ellipsis: { tooltip: true },
    render: (row: any) => h('span', { style: 'color: #303133; cursor: pointer' }, row.title),
  },
  { title: '发布日期', key: 'publish_date', width: 110, sorter: 'default' },
  { title: '抓取时间', key: 'scraped_at', width: 170 },
]

async function loadSentimentStatus() {
  sentimentLoading.value = true
  try {
    const { data } = await getSentimentStatus()
    sentSources.value = data.sources
    sentTotal.value = data.total
    categories.value = data.categories || []
  } finally {
    sentimentLoading.value = false
  }
}

async function loadAnalysisStats() {
  try {
    const { data } = await getSentimentAnalysisStats()
    analysisStats.value = data
  } catch {}
}

async function loadArticles(page = 1) {
  articleLoading.value = true
  articlePage.value = page
  try {
    const params: Record<string, any> = { page }
    if (currentSource.value) params.source = currentSource.value
    if (currentCategory.value) params.category = currentCategory.value
    if (dateRange.value?.[0]) params.start_date = formatDate(dateRange.value[0])
    if (dateRange.value?.[1]) params.end_date = formatDate(dateRange.value[1])
    if (keyword.value.trim()) params.keyword = keyword.value.trim()
    const { data } = await getSentimentArticles(params)
    articles.value = data.articles
    articleTotal.value = data.total
  } finally {
    articleLoading.value = false
  }
}

async function scrapeSource(source: string) {
  scraping.value = true
  try {
    if (source === 'research_report') {
      const { data } = await startDownload('update_reports')
      taskStore.trackTask(data.task_id, '刷新券商研报')
      message.success('券商研报刷新任务已启动')
    } else {
      const { data } = await startSentimentDownload(source)
      taskStore.trackTask(data.task_id, `抓取 ${source}`)
      message.success(`${source} 抓取任务已启动`)
    }
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    scraping.value = false
  }
}

async function scrapeSourceIncremental(source: string) {
  scraping.value = true
  try {
    if (source === 'research_report') {
      const { data } = await startDownload('update_reports')
      taskStore.trackTask(data.task_id, '增量刷新券商研报')
      message.success('券商研报增量刷新任务已启动')
    } else {
      const { data } = await startSentimentDownload(source, undefined, true)
      taskStore.trackTask(data.task_id, `增量更新 ${source}`)
      message.success(`${source} 增量更新任务已启动`)
    }
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    scraping.value = false
  }
}

async function scrapeSourceBackfill(source: string) {
  scraping.value = true
  try {
    if (source === 'research_report') {
      const { data } = await startDownload('backfill_reports')
      taskStore.trackTask(data.task_id, '补录券商研报')
      message.success('券商研报补录任务已启动')
    } else {
      const { data } = await startSentimentDownload(source, undefined, false, true)
      taskStore.trackTask(data.task_id, `补录 ${source}`)
      message.success(`${source} 补录任务已启动`)
    }
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    scraping.value = false
  }
}

async function scrapeAll() {
  scraping.value = true
  try {
    const { data } = await startSentimentDownloadAndAnalyze()
    taskStore.trackTask(data.task_id, '全量抓取+分析')
    message.success('全量抓取+分析任务已启动')
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    scraping.value = false
  }
}

async function backfillAnalyze() {
  scraping.value = true
  try {
    const { data } = await startSentimentBackfillAnalyze()
    taskStore.trackTask(data.task_id, '补录分析')
    message.success('补录分析任务已启动')
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    scraping.value = false
  }
}

async function backfillContent() {
  scraping.value = true
  try {
    const { data } = await startSentimentBackfillContent()
    taskStore.trackTask(data.task_id, '补录全文')
    message.success('补录全文任务已启动')
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    scraping.value = false
  }
}

function filterBySource(source: string) {
  currentSource.value = currentSource.value === source ? '' : source
  mainTab.value = 'articles'
  loadArticles(1)
}

function clearFilters() {
  currentSource.value = ''
  currentCategory.value = null
  dateRange.value = null
  keyword.value = ''
  loadArticles(1)
}

function onArticleSearch() {
  loadArticles(1)
}

function openArticle(row: any) {
  selectedArticle.value = row
  drawerVisible.value = true
}

function articleRowProps(row: any) {
  return { style: 'cursor: pointer', onClick: () => openArticle(row) }
}

function getTierTagType(tier: number): 'error' | 'warning' | 'info' | 'success' | 'default' {
  if (tier <= 2) return 'error'
  if (tier === 3) return 'warning'
  if (tier === 4) return 'success'
  if (tier === 6) return 'info'
  if (tier === 7) return 'info'
  return 'default'
}

// ============================================================
// Generic data browser
// ============================================================
const browserVisible = ref(false)
const browserTable = ref('')
const browserLabel = ref('')
const browserColumns = ref<DataTableColumns>([])
const browserRows = ref<any[]>([])
const browserTotal = ref(0)
const browserPage = ref(1)
const browserPageSize = ref(50)
const browserLoading = ref(false)
const browserKeyword = ref('')

function openBrowser(table: string) {
  browserTable.value = table
  browserLabel.value = table
  browserPage.value = 1
  browserKeyword.value = ''
  browserVisible.value = true
  loadBrowserData()
}

async function loadBrowserData(page = 1) {
  browserLoading.value = true
  browserPage.value = page
  try {
    const { data } = await browseData(browserTable.value, {
      page,
      page_size: browserPageSize.value,
      keyword: browserKeyword.value || undefined,
    })
    browserLabel.value = data.label || data.table
    browserTotal.value = data.total
    browserRows.value = data.rows
    // Dynamic columns from API
    browserColumns.value = (data.columns as string[]).map((col: string) => ({
      title: col,
      key: col,
      width: col.length > 12 ? 180 : 120,
      ellipsis: { tooltip: true },
    }))
  } catch (e: any) {
    message.error(e.response?.data?.error || '加载失败')
    browserRows.value = []
    browserTotal.value = 0
  } finally {
    browserLoading.value = false
  }
}

function onBrowserSearch() {
  loadBrowserData(1)
}

// ============================================================
// Main tab change handler
// ============================================================
function onMainTabChange(tab: string) {
  mainTab.value = tab
  if (tab === 'articles') {
    loadArticles(articlePage.value)
  }
}

onMounted(() => {
  loadStatus()
  loadSentimentStatus()
  loadAnalysisStats()
})
</script>

<template>
  <div>
    <n-tabs type="line" :value="mainTab" @update:value="onMainTabChange" style="margin-bottom: 16px">
      <n-tab name="operations">数据操作</n-tab>
      <n-tab name="articles">文章列表</n-tab>
      <n-tab name="tables">数据表状态</n-tab>
    </n-tabs>

    <!-- ========== Tab: 数据操作 ========== -->
    <div v-show="mainTab === 'operations'">
      <!-- Quick actions -->
      <n-card hoverable style="margin-bottom: 20px" title="快捷操作">
        <n-space size="small">
          <n-button size="small" type="primary" @click="runInit" :disabled="running">
            <template #icon><n-icon><SettingsOutline /></n-icon></template>
            初始化数据库
          </n-button>
          <n-button size="small" type="success" @click="runFullDownload" :disabled="running">
            <template #icon><n-icon><DownloadOutline /></n-icon></template>
            A股全量下载
          </n-button>
          <n-button size="small" type="warning" @click="runUpdate" :disabled="running">
            <template #icon><n-icon><RefreshOutline /></n-icon></template>
            A股增量更新
          </n-button>
          <n-divider vertical />
          <n-button size="small" type="success" @click="runUSFullDownload" :disabled="running">
            <template #icon><n-icon><DownloadOutline /></n-icon></template>
            美股全量下载
          </n-button>
          <n-button size="small" type="warning" @click="runUSUpdate" :disabled="running">
            <template #icon><n-icon><RefreshOutline /></n-icon></template>
            美股增量更新
          </n-button>
        </n-space>
      </n-card>

      <!-- Data operations table -->
      <n-card hoverable title="数据操作">
        <n-data-table :columns="opColumns" :data="dataTypes" :row-key="(r: any) => r.key" size="small" :bordered="false" :default-expanded-row-keys="['sentiment']" />
      </n-card>

    </div>

    <!-- ========== Tab: 文章列表 ========== -->
    <div v-show="mainTab === 'articles'">
      <n-card hoverable style="margin-bottom: 20px">
        <n-space>
          <n-input
            v-model:value="keyword"
            placeholder="搜索标题关键词"
            clearable
            style="width: 220px"
            @keyup.enter="onArticleSearch"
            @clear="onArticleSearch"
          >
            <template #prefix><n-icon><SearchOutline /></n-icon></template>
          </n-input>
          <n-select
            v-model:value="currentCategory"
            :options="categories.map(c => ({ label: c, value: c }))"
            placeholder="栏目分类"
            clearable
            style="width: 160px"
            @update:value="onArticleSearch"
          />
          <n-date-picker
            type="daterange"
            v-model:value="dateRange"
            clearable
            @update:value="onArticleSearch"
            style="width: 280px"
          />
          <n-button type="primary" @click="onArticleSearch">查询</n-button>
          <n-button @click="clearFilters">重置</n-button>
        </n-space>

        <div v-if="currentSource || currentCategory || keyword || dateRange" style="margin-top: 10px">
          <span style="font-size: 12px; color: #909399; margin-right: 8px">筛选条件:</span>
          <n-tag v-if="currentSource" size="small" closable @close="currentSource = ''; loadArticles(1)" style="margin-right: 6px">
            来源: {{ currentSource }}
          </n-tag>
          <n-tag v-if="currentCategory" size="small" closable @close="currentCategory = null; loadArticles(1)" style="margin-right: 6px">
            栏目: {{ currentCategory }}
          </n-tag>
          <n-tag v-if="keyword" size="small" closable @close="keyword = ''; loadArticles(1)" style="margin-right: 6px">
            关键词: {{ keyword }}
          </n-tag>
          <n-tag v-if="dateRange" size="small" closable @close="dateRange = null; loadArticles(1)">
            日期范围已选
          </n-tag>
        </div>
      </n-card>

      <n-card hoverable>
        <template #header>
          <div style="display: flex; justify-content: space-between; align-items: center; width: 100%">
            <span>
              文章列表
              <span style="font-size: 13px; color: #909399; margin-left: 8px">共 {{ articleTotal }} 篇</span>
            </span>
            <n-button text @click="loadArticles(articlePage)">
              <template #icon><n-icon><RefreshOutline /></n-icon></template>
              刷新
            </n-button>
          </div>
        </template>

        <n-data-table
          :columns="articleColumns"
          :data="articles"
          :loading="articleLoading"
          :row-props="articleRowProps"
          striped
          size="small"
          :row-key="(row: any) => row.url || row.title"
        />

        <div style="margin-top: 12px; text-align: right">
          <n-pagination
            v-model:page="articlePage"
            :item-count="articleTotal"
            :page-size="20"
            show-quick-jumper
            @update:page="loadArticles"
          />
        </div>
      </n-card>
    </div>

    <!-- ========== Tab: 数据表状态 ========== -->
    <div v-show="mainTab === 'tables'">
      <n-card hoverable>
        <template #header>
          <div style="display: flex; justify-content: space-between; align-items: center; width: 100%">
            <span>数据表状态</span>
            <n-button text @click="loadStatus" :loading="loading">
              <template #icon><n-icon><RefreshOutline /></n-icon></template>
              刷新
            </n-button>
          </div>
        </template>
        <n-data-table :columns="statusColumns" :data="tables" :loading="loading" striped size="small" />
      </n-card>
    </div>

    <!-- ========== Article detail drawer ========== -->
    <n-drawer v-model:show="drawerVisible" :width="520" placement="right">
      <n-drawer-content v-if="selectedArticle" closable>
        <template #header>文章详情</template>

        <h2 style="margin: 0 0 16px 0; font-size: 20px; line-height: 1.4; color: #303133">
          {{ selectedArticle.title }}
        </h2>

        <n-space style="margin-bottom: 16px" align="center">
          <n-tag :type="getTierTagType(selectedArticle.tier)" size="small">
            {{ selectedArticle.source }}
          </n-tag>
          <span v-if="selectedArticle.category" style="font-size: 13px; color: #909399">
            {{ selectedArticle.category }}
          </span>
          <span style="font-size: 13px; color: #909399">
            {{ selectedArticle.publish_date }}
          </span>
        </n-space>

        <n-divider style="margin: 12px 0" />

        <div v-if="selectedArticle.summary" style="margin-bottom: 20px">
          <div style="font-size: 13px; font-weight: 600; color: #606266; margin-bottom: 8px">摘要</div>
          <div class="drawer-summary">
            {{ selectedArticle.summary }}
          </div>
        </div>
        <div v-else style="margin-bottom: 20px; color: #909399; font-size: 13px">
          暂无摘要内容
        </div>

        <n-button
          v-if="selectedArticle.url"
          type="primary"
          tag="a"
          :href="selectedArticle.url"
          target="_blank"
          style="margin-bottom: 16px"
        >
          <template #icon><n-icon><OpenOutline /></n-icon></template>
          查看原文
        </n-button>

        <n-divider style="margin: 12px 0" />

        <div style="font-size: 12px; color: #909399">
          抓取时间：{{ selectedArticle.scraped_at || '-' }}
        </div>
      </n-drawer-content>
    </n-drawer>

    <!-- ========== Generic data browser drawer ========== -->
    <n-drawer v-model:show="browserVisible" :width="900" placement="right">
      <n-drawer-content closable>
        <template #header>
          {{ browserLabel }}
          <span style="font-size: 13px; color: #909399; margin-left: 8px">
            共 {{ browserTotal.toLocaleString() }} 条
          </span>
        </template>

        <n-space style="margin-bottom: 12px">
          <n-input
            v-model:value="browserKeyword"
            placeholder="关键词搜索"
            clearable
            style="width: 240px"
            @keyup.enter="onBrowserSearch"
            @clear="onBrowserSearch"
          >
            <template #prefix><n-icon><SearchOutline /></n-icon></template>
          </n-input>
          <n-button type="primary" size="small" @click="onBrowserSearch">搜索</n-button>
        </n-space>

        <n-data-table
          :columns="browserColumns"
          :data="browserRows"
          :loading="browserLoading"
          striped
          size="small"
          :max-height="500"
          virtual-scroll
        />

        <div style="margin-top: 12px; text-align: right">
          <n-pagination
            v-model:page="browserPage"
            :item-count="browserTotal"
            :page-size="browserPageSize"
            show-quick-jumper
            @update:page="loadBrowserData"
          />
        </div>
      </n-drawer-content>
    </n-drawer>
  </div>
</template>

<style scoped>
.drawer-summary {
  padding: 12px 16px;
  background: #f5f7fa;
  border-radius: 6px;
  font-size: 14px;
  color: #606266;
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
