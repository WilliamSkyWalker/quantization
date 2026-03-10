<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed, watch, h } from 'vue'
import { useMessage } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import {
  startMonitor, stopMonitor, getMonitorStatus, getAlerts, markAlertRead, triggerMockAlert,
  runBacktest, getBacktestResult, getImpactOverview, deleteMockAlerts, runUsStockPnlFromDb, runASharePnlFromDb,
} from '../api/polymarket'
import { useTaskStore } from '../stores/task'
import { colors, semanticColor } from '../theme'

const message = useMessage()
const taskStore = useTaskStore()

// Tab
const activeTab = ref('monitor')

// ============================================================
// Monitor Tab State
// ============================================================
const loading = ref(false)
const monitorRunning = ref(false)
const markets = ref<any[]>([])
const alerts = ref<any[]>([])
const alertTotal = ref(0)
const alertPage = ref(1)
const alertPageSize = ref(20)
const selectedAlert = ref<any>(null)
const showDetail = ref(false)
const toggleLoading = ref(false)

// Mock test
const showMockDialog = ref(false)
const mockLoading = ref(false)
const mockForm = ref({
  question: '',
  description: '',
  category: 'politics',
  price_before: 0.1,
  price_after: 0.6,
  alert_type: 'spike_5m',
})

const mockPresets = [
  {
    label: '美国打击伊朗',
    question: 'Will the US launch military strikes on Iran before July 2026?',
    description: 'This market resolves YES if the United States conducts direct military strikes against targets within Iran.',
    category: 'politics',
    price_before: 0.12,
    price_after: 0.67,
  },
  {
    label: '美联储紧急降息',
    question: 'Will the Fed cut rates by 50+ basis points at the next meeting?',
    description: 'Resolves YES if the Federal Reserve cuts the federal funds rate by 50 or more basis points.',
    category: 'economics',
    price_before: 0.08,
    price_after: 0.55,
  },
  {
    label: '中美关税升级',
    question: 'Will the US impose 60%+ tariffs on all Chinese imports before 2027?',
    description: 'Resolves YES if the US implements tariffs of 60% or higher on all imports from China.',
    category: 'politics',
    price_before: 0.25,
    price_after: 0.72,
  },
]

function applyPreset(preset: (typeof mockPresets)[0]) {
  mockForm.value.question = preset.question
  mockForm.value.description = preset.description
  mockForm.value.category = preset.category
  mockForm.value.price_before = preset.price_before
  mockForm.value.price_after = preset.price_after
}

async function submitMock() {
  if (!mockForm.value.question) {
    message.warning('请输入事件问题')
    return
  }
  mockLoading.value = true
  try {
    const { data } = await triggerMockAlert(mockForm.value)
    taskStore.trackTask(data.task_id, '模拟告警: ' + mockForm.value.question.substring(0, 30))
    message.success(data.message)
    showMockDialog.value = false
    setTimeout(() => loadAlerts(), 3000)
  } catch (e: any) {
    message.error('触发失败: ' + (e.response?.data?.error || e.message))
  } finally {
    mockLoading.value = false
  }
}

const deleteMockLoading = ref(false)
async function doDeleteMock() {
  deleteMockLoading.value = true
  try {
    const { data } = await deleteMockAlerts()
    message.success(`已删除 ${data.deleted_alerts} 条 Mock 告警、${data.deleted_events} 个 Mock 事件`)
    loadAlerts()
    // 如果历史影响 tab 已加载，刷新
    if (impactData.value) loadImpact()
  } catch (e: any) {
    message.error('删除失败: ' + (e.response?.data?.error || e.message))
  } finally {
    deleteMockLoading.value = false
  }
}

// WebSocket
let ws: WebSocket | null = null
let wsReconnectTimer: ReturnType<typeof setTimeout> | null = null

function connectWs() {
  if (ws && ws.readyState <= 1) return
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}/ws/polymarket/`)

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data)
      if (msg.type === 'price_update') {
        handlePriceUpdate(msg.data)
      } else if (msg.type === 'alert') {
        handleNewAlert(msg.data)
      }
    } catch {}
  }

  ws.onclose = () => {
    if (monitorRunning.value) {
      wsReconnectTimer = setTimeout(connectWs, 3000)
    }
  }

  ws.onerror = () => ws?.close()
}

function disconnectWs() {
  if (wsReconnectTimer) {
    clearTimeout(wsReconnectTimer)
    wsReconnectTimer = null
  }
  if (ws) {
    ws.close()
    ws = null
  }
}

function handlePriceUpdate(data: any) {
  const idx = markets.value.findIndex((m) => m.condition_id === data.condition_id)
  if (idx >= 0) {
    markets.value[idx].yes_price = data.yes_price
  }
}

function handleNewAlert(data: any) {
  alerts.value.unshift(data)
  alertTotal.value++
  message.info('新告警: ' + (data.question || '').substring(0, 60))
}

// Monitor API calls
async function loadStatus() {
  try {
    const { data } = await getMonitorStatus()
    monitorRunning.value = data.is_running
    markets.value = data.markets || []
  } catch {}
}

async function loadAlerts() {
  try {
    const { data } = await getAlerts({ page: alertPage.value, page_size: alertPageSize.value })
    alerts.value = data.items || []
    alertTotal.value = data.total || 0
  } catch {}
}

async function toggleMonitor() {
  toggleLoading.value = true
  try {
    if (monitorRunning.value) {
      await stopMonitor()
      monitorRunning.value = false
      disconnectWs()
      message.success('监控已停止')
    } else {
      await startMonitor()
      monitorRunning.value = true
      connectWs()
      message.success('监控已启动')
    }
  } catch (e: any) {
    message.error('操作失败: ' + (e.response?.data?.error || e.message))
  } finally {
    toggleLoading.value = false
  }
}

async function doMarkRead(alertId: number) {
  try {
    await markAlertRead(alertId)
    const item = alerts.value.find((a) => a.id === alertId)
    if (item) item.is_read = true
  } catch {}
}

function openAlertDetail(row: any) {
  selectedAlert.value = row
  showDetail.value = true
  if (!row.is_read) doMarkRead(row.id)
}

function handleAlertPageChange(page: number) {
  alertPage.value = page
  loadAlerts()
}

// Format helpers
function formatPrice(val: number | null) {
  if (val == null) return '-'
  return (val * 100).toFixed(1) + '%'
}

function formatChange(val: number | null) {
  if (val == null) return '-'
  const pct = (val * 100).toFixed(1)
  return (val >= 0 ? '+' : '') + pct + '%'
}

function formatVolume(val: number | null) {
  if (val == null) return '-'
  if (val >= 1_000_000) return '$' + (val / 1_000_000).toFixed(1) + 'M'
  if (val >= 1_000) return '$' + (val / 1_000).toFixed(0) + 'K'
  return '$' + val.toFixed(0)
}

function alertTypeLabel(type: string) {
  const map: Record<string, string> = {
    spike_5m: '5分钟异动',
    spike_1h: '1小时异动',
    spike_24h: '24小时异动',
  }
  return map[type] || type
}

function directionLabel(dir: string) {
  return dir === 'bullish' ? '利好' : '利空'
}

function directionTag(dir: string) {
  return dir === 'bullish' ? 'success' : 'error'
}

// Monitor table columns
const marketColumns: DataTableColumns = [
  { title: '事件问题', key: 'question', ellipsis: { tooltip: true }, width: 400 },
  { title: '分类', key: 'category', width: 120 },
  {
    title: 'YES 赔率',
    key: 'yes_price',
    width: 100,
    render: (row: any) => formatPrice(row.yes_price),
  },
  {
    title: '交易量',
    key: 'volume',
    width: 100,
    render: (row: any) => formatVolume(row.volume),
  },
]

const alertColumns: DataTableColumns = [
  {
    title: '类型',
    key: 'alert_type',
    width: 110,
    render: (row: any) => alertTypeLabel(row.alert_type),
  },
  { title: '事件', key: 'question', ellipsis: { tooltip: true }, width: 300 },
  {
    title: '赔率变动',
    key: 'price_change',
    width: 150,
    render: (row: any) =>
      `${formatPrice(row.price_before)} → ${formatPrice(row.price_after)} (${formatChange(row.price_change)})`,
  },
  {
    title: '受影响股票',
    key: 'affected_tickers',
    width: 180,
    render: (row: any) => {
      const us = (row.affected_tickers || []).map((t: any) => t.ticker)
      const cn = (row.affected_a_shares || []).map((s: any) => s.name)
      const all = [...us, ...cn]
      return all.length ? all.join(', ') : '-'
    },
  },
  {
    title: '情感',
    key: 'llm_sentiment',
    width: 80,
    render: (row: any) => (row.llm_sentiment != null ? row.llm_sentiment.toFixed(2) : '-'),
  },
  {
    title: '时间',
    key: 'created_at',
    width: 160,
    render: (row: any) => (row.created_at ? new Date(row.created_at).toLocaleString() : '-'),
  },
]

const unreadCount = computed(() => alerts.value.filter((a) => !a.is_read).length)

// ============================================================
// Backtest Tab State
// ============================================================
const btRunLoading = ref(false)
const btResult = ref<any>(null)
const btTaskId = ref<string | null>(null)
const btSelectedAlert = ref<any>(null)
const btShowAlertDetail = ref(false)

// Backtest config
const btConfig = ref({
  use_llm: true,
  spike_5m: 0.05,
  spike_1h: 0.15,
  spike_24h: 0.25,
})

// 回测任务实时状态（从 task store 响应式获取）
const btTask = computed(() => btTaskId.value ? taskStore.getTask(btTaskId.value) : undefined)

// Watch task store for backtest completion (替代轮询)
watch(
  () => btTask.value?.status,
  (status) => {
    if (!status || !btTaskId.value) return
    if (status === 'completed') {
      btResult.value = btTask.value?.result
      btRunLoading.value = false
      message.success('量化回测完成')
    } else if (status === 'failed') {
      btRunLoading.value = false
      message.error('量化回测失败: ' + (btTask.value?.error || ''))
    } else if (status === 'cancelled') {
      btRunLoading.value = false
    }
  },
)

async function doRunBacktest() {
  btRunLoading.value = true
  btResult.value = null
  btTaskId.value = null
  try {
    const payload: any = {
      use_llm: btConfig.value.use_llm,
      spike_5m: btConfig.value.spike_5m,
      spike_1h: btConfig.value.spike_1h,
      spike_24h: btConfig.value.spike_24h,
    }
    const { data } = await runBacktest(payload)
    btTaskId.value = data.task_id
    taskStore.trackTask(data.task_id, 'Polymarket 量化回测')
    message.success('已提交量化回测任务')
  } catch (e: any) {
    message.error('量化回测失败: ' + (e.response?.data?.error || e.message))
    btRunLoading.value = false
  }
}

function openBtAlertDetail(row: any) {
  btSelectedAlert.value = row
  btShowAlertDetail.value = true
}


// Backtest table columns
const btAlertColumns: DataTableColumns = [
  {
    title: '类型',
    key: 'alert_type',
    width: 100,
    render: (row: any) => alertTypeLabel(row.alert_type),
  },
  { title: '事件', key: 'question', ellipsis: { tooltip: true }, width: 280 },
  {
    title: '赔率变动',
    key: 'price_change',
    width: 150,
    render: (row: any) =>
      `${formatPrice(row.price_before)} → ${formatPrice(row.price_after)} (${formatChange(row.price_change)})`,
  },
  {
    title: '受影响股票',
    key: 'affected_tickers',
    width: 180,
    render: (row: any) => {
      const us = (row.affected_tickers || []).map((t: any) => t.ticker)
      const cn = (row.affected_a_shares || []).map((s: any) => s.name)
      const all = [...us, ...cn]
      return all.length ? all.join(', ') : '-'
    },
  },
  {
    title: '情感',
    key: 'llm_sentiment',
    width: 70,
    render: (row: any) => (row.llm_sentiment != null ? row.llm_sentiment.toFixed(2) : '-'),
  },
  {
    title: '时间',
    key: 'timestamp',
    width: 140,
    render: (row: any) => (row.timestamp ? new Date(row.timestamp).toLocaleString() : '-'),
  },
]

// ============================================================
// US Stock P&L State (within backtest tab)
// ============================================================
const pnlRunLoading = ref(false)
const pnlTaskId = ref<string | null>(null)
const pnlResult = ref<any>(null)
const pnlConfig = ref({
  holding_days: 5,
  min_confidence: 0.0,
})

const pnlTask = computed(() => pnlTaskId.value ? taskStore.getTask(pnlTaskId.value) : undefined)

watch(
  () => pnlTask.value?.status,
  (status) => {
    if (!status || !pnlTaskId.value) return
    if (status === 'completed') {
      pnlResult.value = pnlTask.value?.result
      pnlRunLoading.value = false
      message.success('美股新闻信号回测完成')
    } else if (status === 'failed') {
      pnlRunLoading.value = false
      message.error('美股新闻信号回测失败: ' + (pnlTask.value?.error || ''))
    } else if (status === 'cancelled') {
      pnlRunLoading.value = false
    }
  },
)

async function doRunPnl() {
  pnlRunLoading.value = true
  pnlResult.value = null
  pnlTaskId.value = null
  try {
    const { data } = await runUsStockPnlFromDb({
      holding_days: pnlConfig.value.holding_days,
      min_confidence: pnlConfig.value.min_confidence,
    })
    pnlTaskId.value = data.task_id
    taskStore.trackTask(data.task_id, '美股新闻信号回测')
    message.success('已提交美股新闻信号回测任务')
  } catch (e: any) {
    message.error('美股新闻信号回测失败: ' + (e.response?.data?.error || e.message))
    pnlRunLoading.value = false
  }
}

// P&L trade table columns (共用渲染函数)
function renderReturnPct(row: any, key: string, bold = true) {
  const v = row[key]
  if (v == null) return '-'
  const color = semanticColor(v)
  return h('span', { style: { color, fontWeight: bold ? 600 : 400 } }, `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`)
}

const pnlTradeColumns: DataTableColumns = [
  { title: 'Ticker', key: 'ticker', width: 80 },
  {
    title: '方向',
    key: 'direction',
    width: 70,
    render: (row: any) => row.direction === 'bullish' ? '看多' : '看空',
  },
  { title: '入场日', key: 'entry_date', width: 100 },
  {
    title: '入场价',
    key: 'entry_price',
    width: 85,
    render: (row: any) => row.entry_price != null ? `$${row.entry_price.toFixed(2)}` : '-',
  },
  { title: '出场日', key: 'exit_date', width: 100 },
  {
    title: '出场价',
    key: 'exit_price',
    width: 85,
    render: (row: any) => row.exit_price != null ? `$${row.exit_price.toFixed(2)}` : '-',
  },
  {
    title: '收益%',
    key: 'return_pct',
    width: 80,
    sorter: (a: any, b: any) => (a.return_pct ?? 0) - (b.return_pct ?? 0),
    render: (row: any) => renderReturnPct(row, 'return_pct'),
  },
  {
    title: 'BM%',
    key: 'benchmark_pct',
    width: 75,
    render: (row: any) => renderReturnPct(row, 'benchmark_pct', false),
  },
  {
    title: 'Alpha',
    key: 'alpha_pct',
    width: 75,
    sorter: (a: any, b: any) => (a.alpha_pct ?? 0) - (b.alpha_pct ?? 0),
    render: (row: any) => renderReturnPct(row, 'alpha_pct'),
  },
  {
    title: '持仓天',
    key: 'holding_days',
    width: 70,
  },
  {
    title: '事件',
    key: 'event_question',
    ellipsis: { tooltip: true },
    width: 250,
  },
]

// ============================================================
// A-Share P&L State (within backtest tab)
// ============================================================
const cnPnlRunLoading = ref(false)
const cnPnlTaskId = ref<string | null>(null)
const cnPnlResult = ref<any>(null)
const cnPnlConfig = ref({
  holding_days: 5,
  min_confidence: 0.0,
})

const cnPnlTask = computed(() => cnPnlTaskId.value ? taskStore.getTask(cnPnlTaskId.value) : undefined)

watch(
  () => cnPnlTask.value?.status,
  (status) => {
    if (!status || !cnPnlTaskId.value) return
    if (status === 'completed') {
      cnPnlResult.value = cnPnlTask.value?.result
      cnPnlRunLoading.value = false
      message.success('A股新闻信号回测完成')
    } else if (status === 'failed') {
      cnPnlRunLoading.value = false
      message.error('A股新闻信号回测失败: ' + (cnPnlTask.value?.error || ''))
    } else if (status === 'cancelled') {
      cnPnlRunLoading.value = false
    }
  },
)

async function doRunCnPnl() {
  cnPnlRunLoading.value = true
  cnPnlResult.value = null
  cnPnlTaskId.value = null
  try {
    const { data } = await runASharePnlFromDb({
      holding_days: cnPnlConfig.value.holding_days,
      min_confidence: cnPnlConfig.value.min_confidence,
    })
    cnPnlTaskId.value = data.task_id
    taskStore.trackTask(data.task_id, 'A股新闻信号回测')
    message.success('已提交 A 股新闻信号回测任务')
  } catch (e: any) {
    message.error('A股新闻信号回测失败: ' + (e.response?.data?.error || e.message))
    cnPnlRunLoading.value = false
  }
}

// A-share P&L trade table columns
const cnPnlTradeColumns: DataTableColumns = [
  {
    title: '股票',
    key: 'name',
    width: 80,
  },
  {
    title: '代码',
    key: 'ticker',
    width: 100,
  },
  {
    title: '方向',
    key: 'direction',
    width: 70,
    render: (row: any) => row.direction === 'bullish' ? '看多' : '看空',
  },
  { title: '入场日', key: 'entry_date', width: 100 },
  {
    title: '入场价',
    key: 'entry_price',
    width: 85,
    render: (row: any) => row.entry_price != null ? `¥${row.entry_price.toFixed(2)}` : '-',
  },
  { title: '出场日', key: 'exit_date', width: 100 },
  {
    title: '出场价',
    key: 'exit_price',
    width: 85,
    render: (row: any) => row.exit_price != null ? `¥${row.exit_price.toFixed(2)}` : '-',
  },
  {
    title: '收益%',
    key: 'return_pct',
    width: 80,
    sorter: (a: any, b: any) => (a.return_pct ?? 0) - (b.return_pct ?? 0),
    render: (row: any) => renderReturnPct(row, 'return_pct'),
  },
  {
    title: 'BM%',
    key: 'benchmark_pct',
    width: 75,
    render: (row: any) => renderReturnPct(row, 'benchmark_pct', false),
  },
  {
    title: 'Alpha',
    key: 'alpha_pct',
    width: 75,
    sorter: (a: any, b: any) => (a.alpha_pct ?? 0) - (b.alpha_pct ?? 0),
    render: (row: any) => renderReturnPct(row, 'alpha_pct'),
  },
  {
    title: '持仓天',
    key: 'holding_days',
    width: 70,
  },
  {
    title: '事件',
    key: 'event_question',
    ellipsis: { tooltip: true },
    width: 250,
  },
]

// ============================================================
// Impact Tab State
// ============================================================
const impactLoading = ref(false)
const impactData = ref<any>(null)
const impactDays = ref(365)
const impactMinConf = ref(0)
const impactSelectedAlert = ref<any>(null)
const impactShowDetail = ref(false)

async function loadImpact() {
  impactLoading.value = true
  try {
    const { data } = await getImpactOverview({ days: impactDays.value, min_confidence: impactMinConf.value })
    impactData.value = data
  } catch (e: any) {
    message.error('加载影响数据失败: ' + (e.response?.data?.error || e.message))
  } finally {
    impactLoading.value = false
  }
}

function openImpactAlertDetail(row: any) {
  impactSelectedAlert.value = row
  impactShowDetail.value = true
}

// 情感色值：-1 红，0 灰，+1 绿
function sentimentColor(val: number | null): string {
  if (val == null) return colors.neutral
  if (val > 0.3) return colors.positive
  if (val > 0.1) return '#66ba76'
  if (val < -0.3) return colors.negative
  if (val < -0.1) return '#e08686'
  return colors.neutral
}

function sentimentBg(val: number | null): string {
  if (val == null) return 'transparent'
  if (val > 0.3) return 'rgba(24,160,88,0.08)'
  if (val > 0.1) return 'rgba(92,184,92,0.06)'
  if (val < -0.3) return 'rgba(208,48,80,0.08)'
  if (val < -0.1) return 'rgba(232,128,128,0.06)'
  return 'transparent'
}

// Impact alert table columns
const impactAlertColumns: DataTableColumns = [
  {
    title: '类型',
    key: 'alert_type',
    width: 100,
    render: (row: any) => alertTypeLabel(row.alert_type),
  },
  {
    title: '分类',
    key: 'category',
    width: 80,
  },
  { title: '事件', key: 'question', ellipsis: { tooltip: true }, width: 280 },
  {
    title: '赔率变动',
    key: 'price_change',
    width: 140,
    render: (row: any) =>
      `${formatPrice(row.price_before)} → ${formatPrice(row.price_after)} (${formatChange(row.price_change)})`,
  },
  {
    title: '情感',
    key: 'llm_sentiment',
    width: 70,
    render: (row: any) => (row.llm_sentiment != null ? row.llm_sentiment.toFixed(2) : '-'),
  },
  {
    title: '受影响行业',
    key: 'affected_sw_industries',
    width: 160,
    render: (row: any) => {
      const inds = row.affected_sw_industries || []
      return inds.length ? inds.join(', ') : '-'
    },
  },
  {
    title: '时间',
    key: 'created_at',
    width: 140,
    render: (row: any) => (row.created_at ? new Date(row.created_at).toLocaleString() : '-'),
  },
]

// ============================================================
// Lifecycle
// ============================================================
onMounted(async () => {
  loading.value = true
  await Promise.allSettled([loadStatus(), loadAlerts()])
  loading.value = false
  if (monitorRunning.value) connectWs()
})

onUnmounted(() => {
  disconnectWs()
})

async function loadBacktestResult() {
  try {
    const { data } = await getBacktestResult()
    if (data?.alerts?.length) {
      btResult.value = data
    }
  } catch { /* ignore */ }
}

function handleTabChange(tab: string) {
  activeTab.value = tab
  if (tab === 'backtest' && !btResult.value && !btRunLoading.value) {
    loadBacktestResult()
  }
  if (tab === 'impact' && !impactData.value) {
    loadImpact()
  }
}
</script>

<template>
  <n-tabs :value="activeTab" type="line" @update:value="handleTabChange">
    <!-- ============================================================ -->
    <!-- 实时监控 Tab -->
    <!-- ============================================================ -->
    <n-tab-pane name="monitor" tab="实时监控">
      <n-spin :show="loading">
        <n-space vertical :size="16">
          <!-- 顶部：监控控制 -->
          <n-card size="small">
            <n-space align="center" justify="space-between">
              <n-space align="center" :size="12">
                <n-tag :type="monitorRunning ? 'success' : 'default'" round>
                  {{ monitorRunning ? '运行中' : '已停止' }}
                </n-tag>
                <span :style="{ color: colors.textTertiary, fontSize: '13px' }">
                  正在监控 {{ markets.length }} 个预测市场
                </span>
                <n-badge v-if="unreadCount > 0" :value="unreadCount" type="error">
                  <span style="font-size: 13px">未读告警</span>
                </n-badge>
              </n-space>
              <n-space :size="8">
                <n-popconfirm @positive-click="doDeleteMock">
                  <template #trigger>
                    <n-button quaternary type="error" :loading="deleteMockLoading" size="small">
                      清除Mock数据
                    </n-button>
                  </template>
                  确认删除所有 Mock 告警和事件？
                </n-popconfirm>
                <n-button quaternary type="warning" @click="showMockDialog = true">
                  模拟告警
                </n-button>
                <n-button
                  :type="monitorRunning ? 'error' : 'primary'"
                  :loading="toggleLoading"
                  @click="toggleMonitor"
                >
                  {{ monitorRunning ? '停止监控' : '启动监控' }}
                </n-button>
              </n-space>
            </n-space>
          </n-card>

          <!-- 双栏布局 -->
          <n-grid :cols="2" :x-gap="16" responsive="screen" item-responsive>
            <n-gi span="2 m:1">
              <n-card title="监控市场" size="small" :segmented="{ content: true }">
                <n-data-table
                  :columns="marketColumns"
                  :data="markets"
                  :row-key="(row: any) => row.condition_id"
                  size="small"
                  :max-height="500"
                  :scroll-x="600"
                />
              </n-card>
            </n-gi>

            <n-gi span="2 m:1">
              <n-card title="告警列表" size="small" :segmented="{ content: true }">
                <n-data-table
                  :columns="alertColumns"
                  :data="alerts"
                  :row-key="(row: any) => row.id"
                  size="small"
                  :max-height="500"
                  :scroll-x="800"
                  :row-props="(row: any) => ({
                    style: row.is_read ? '' : 'background: rgba(64,158,255,0.06); cursor: pointer',
                    onClick: () => openAlertDetail(row),
                  })"
                />
                <n-space justify="end" style="margin-top: 12px">
                  <n-pagination
                    v-model:page="alertPage"
                    :page-count="Math.ceil(alertTotal / alertPageSize)"
                    @update:page="handleAlertPageChange"
                    size="small"
                  />
                </n-space>
              </n-card>
            </n-gi>
          </n-grid>
        </n-space>
      </n-spin>
    </n-tab-pane>

    <!-- ============================================================ -->
    <!-- 回测 Tab -->
    <!-- ============================================================ -->
    <n-tab-pane name="backtest" tab="回测">
      <n-space vertical :size="16">
        <!-- 量化回测配置 + 启动 -->
        <n-card size="small" title="量化回测配置">
          <n-grid :cols="4" :x-gap="12" :y-gap="12">
            <n-gi>
              <n-form-item label="5min 阈值" :show-feedback="false">
                <n-input-number v-model:value="btConfig.spike_5m" :min="0.01" :max="1" :step="0.01" size="small" />
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item label="1h 阈值" :show-feedback="false">
                <n-input-number v-model:value="btConfig.spike_1h" :min="0.01" :max="1" :step="0.01" size="small" />
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item label="24h 阈值" :show-feedback="false">
                <n-input-number v-model:value="btConfig.spike_24h" :min="0.01" :max="1" :step="0.01" size="small" />
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item label="LLM 分析" :show-feedback="false">
                <n-switch v-model:value="btConfig.use_llm" />
              </n-form-item>
            </n-gi>
          </n-grid>
          <n-space style="margin-top: 12px" :size="12" align="center">
            <n-button type="primary" :loading="btRunLoading" @click="doRunBacktest">
              启动量化回测
            </n-button>
            <span :style="{ color: colors.textTertiary, fontSize: '13px' }">
              数据准备请前往「数据管理」页
            </span>
          </n-space>
        </n-card>

        <!-- 量化回测进度 -->
        <n-card v-if="btTaskId && btTask && btTask.status === 'running'" size="small" title="量化回测进度">
          <n-progress type="line" :percentage="btTask.progress" :indicator-placement="'inside'" />
          <div :style="{ marginTop: '8px', color: colors.textTertiary, fontSize: '13px' }">{{ btTask.message }}</div>
        </n-card>

        <!-- 回测结果 -->
        <template v-if="btResult">
          <!-- 汇总统计 -->
          <n-card size="small" title="量化回测汇总">
            <n-grid :cols="4" :x-gap="16" :y-gap="12">
              <n-gi>
                <n-statistic label="量化回测市场数" :value="btResult.summary?.total_markets ?? 0" />
              </n-gi>
              <n-gi>
                <n-statistic label="触发告警市场" :value="btResult.summary?.markets_with_alerts ?? 0" />
              </n-gi>
              <n-gi>
                <n-statistic label="总告警数" :value="btResult.summary?.total_alerts ?? 0" />
              </n-gi>
              <n-gi>
                <n-statistic label="LLM 分析数" :value="btResult.summary?.alerts_with_llm ?? 0" />
              </n-gi>
            </n-grid>

            <!-- 告警类型分布 -->
            <n-space style="margin-top: 16px" :size="8">
              <span :style="{ color: colors.textTertiary, fontSize: '13px' }">告警类型分布:</span>
              <n-tag v-for="(count, type) in (btResult.summary?.alert_type_counts || {})" :key="type as string" size="small">
                {{ alertTypeLabel(type as string) }}: {{ count }}
              </n-tag>
            </n-space>

            <!-- 平均情感 -->
            <n-space style="margin-top: 8px" :size="8" v-if="btResult.summary?.avg_sentiment != null">
              <span :style="{ color: colors.textTertiary, fontSize: '13px' }">平均情感:</span>
              <n-tag :type="btResult.summary.avg_sentiment >= 0 ? 'success' : 'error'" size="small">
                {{ btResult.summary.avg_sentiment.toFixed(3) }}
              </n-tag>
              <span :style="{ color: colors.textTertiary, fontSize: '13px' }">平均置信度:</span>
              <n-tag size="small">
                {{ btResult.summary.avg_confidence?.toFixed(3) ?? '-' }}
              </n-tag>
            </n-space>
          </n-card>

          <!-- 高频受影响股票 -->
          <n-grid :cols="2" :x-gap="16" v-if="btResult.summary?.top_us_tickers?.length || btResult.summary?.top_a_shares?.length">
            <n-gi v-if="btResult.summary?.top_us_tickers?.length">
              <n-card size="small" title="高频受影响美股 (Top 10)">
                <n-space vertical :size="4">
                  <div
                    v-for="t in btResult.summary.top_us_tickers"
                    :key="t.ticker"
                    style="display: flex; align-items: center; gap: 8px; padding: 4px 0"
                  >
                    <n-tag size="small" type="info" round>{{ t.ticker }}</n-tag>
                    <n-progress
                      type="line"
                      :percentage="Math.round((t.count / btResult.summary.total_alerts) * 100)"
                      :show-indicator="false"
                      style="flex: 1"
                    />
                    <span :style="{ color: colors.textSecondary, fontSize: '12px', whiteSpace: 'nowrap' }">{{ t.count }} 次</span>
                  </div>
                </n-space>
              </n-card>
            </n-gi>
            <n-gi v-if="btResult.summary?.top_a_shares?.length">
              <n-card size="small" title="高频受影响A股 (Top 10)">
                <n-space vertical :size="4">
                  <div
                    v-for="s in btResult.summary.top_a_shares"
                    :key="s.name"
                    style="display: flex; align-items: center; gap: 8px; padding: 4px 0"
                  >
                    <n-tag size="small" type="warning" round>{{ s.name }}</n-tag>
                    <n-progress
                      type="line"
                      :percentage="Math.round((s.count / btResult.summary.total_alerts) * 100)"
                      :show-indicator="false"
                      style="flex: 1"
                    />
                    <span :style="{ color: colors.textSecondary, fontSize: '12px', whiteSpace: 'nowrap' }">{{ s.count }} 次</span>
                  </div>
                </n-space>
              </n-card>
            </n-gi>
          </n-grid>

          <!-- 市场量化回测详情 -->
          <n-card size="small" title="市场量化回测详情" :segmented="{ content: true }">
            <n-data-table
              :columns="[
                { title: '事件', key: 'question', ellipsis: { tooltip: true }, width: 350 },
                { title: '分类', key: 'category', width: 80 },
                { title: '交易量', key: 'volume', width: 90, render: (row: any) => formatVolume(row.volume) },
                { title: '数据点', key: 'data_points', width: 70 },
                { title: '告警数', key: 'alerts_triggered', width: 70 },
                { title: '价格区间', key: 'price_range', width: 80, render: (row: any) => formatPrice(row.price_range) },
                { title: '起始价', key: 'price_start', width: 70, render: (row: any) => formatPrice(row.price_start) },
                { title: '终止价', key: 'price_end', width: 70, render: (row: any) => formatPrice(row.price_end) },
              ]"
              :data="btResult.markets || []"
              :row-key="(row: any) => row.condition_id"
              size="small"
              :max-height="300"
              :scroll-x="900"
            />
          </n-card>

          <!-- 量化回测告警列表 -->
          <n-card size="small" title="量化回测触发的告警" :segmented="{ content: true }">
            <n-data-table
              :columns="btAlertColumns"
              :data="btResult.alerts || []"
              :row-key="(row: any, idx: number) => idx"
              size="small"
              :max-height="400"
              :scroll-x="900"
              :row-props="(row: any) => ({
                style: 'cursor: pointer',
                onClick: () => openBtAlertDetail(row),
              })"
            />
          </n-card>
        </template>

        <!-- ============================================================ -->
        <!-- 美股新闻信号回测 -->
        <!-- ============================================================ -->

        <!-- 美股新闻信号回测配置 + 按钮 -->
        <n-card size="small" title="美股新闻信号回测">
          <n-grid :cols="3" :x-gap="12" :y-gap="12">
            <n-gi>
              <n-form-item label="持仓天数" :show-feedback="false">
                <n-input-number v-model:value="pnlConfig.holding_days" :min="1" :max="30" :step="1" size="small" />
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item label="最低置信度" :show-feedback="false">
                <n-input-number v-model:value="pnlConfig.min_confidence" :min="0" :max="1" :step="0.1" size="small" />
              </n-form-item>
            </n-gi>
            <n-gi style="display: flex; align-items: flex-end">
              <n-button
                type="primary"
                :loading="pnlRunLoading"
                @click="doRunPnl"
              >
                运行美股新闻信号回测
              </n-button>
            </n-gi>
          </n-grid>
        </n-card>

        <!-- 美股新闻信号回测进度 -->
        <n-card v-if="pnlTaskId && pnlTask && pnlTask.status === 'running'" size="small" title="美股新闻信号回测进度">
          <n-progress type="line" :percentage="pnlTask.progress" :indicator-placement="'inside'" />
          <div :style="{ marginTop: '8px', color: colors.textTertiary, fontSize: '13px' }">{{ pnlTask.message }}</div>
        </n-card>

        <!-- P&L 结果 -->
        <template v-if="pnlResult">
          <!-- 汇总统计 -->
          <n-card size="small" title="美股新闻信号回测汇总">
            <n-grid :cols="6" :x-gap="12" :y-gap="12" responsive="screen" item-responsive>
              <n-gi span="6 m:1">
                <n-statistic label="总交易数" :value="pnlResult.summary?.total_trades ?? 0" />
              </n-gi>
              <n-gi span="6 m:1">
                <n-statistic label="胜率">
                  <template #default>
                    <span :style="{ color: (pnlResult.summary?.win_rate ?? 0) >= 0.5 ? colors.positive : colors.negative }">
                      {{ ((pnlResult.summary?.win_rate ?? 0) * 100).toFixed(1) }}%
                    </span>
                  </template>
                </n-statistic>
              </n-gi>
              <n-gi span="6 m:1">
                <n-statistic label="平均收益">
                  <template #default>
                    <span :style="{ color: (pnlResult.summary?.avg_return_pct ?? 0) >= 0 ? colors.positive : colors.negative }">
                      {{ (pnlResult.summary?.avg_return_pct ?? 0) >= 0 ? '+' : '' }}{{ (pnlResult.summary?.avg_return_pct ?? 0).toFixed(2) }}%
                    </span>
                  </template>
                </n-statistic>
              </n-gi>
              <n-gi span="6 m:1">
                <n-statistic label="Sharpe">
                  <template #default>
                    <span :style="{ color: (pnlResult.summary?.sharpe_ratio ?? 0) >= 0 ? colors.positive : colors.negative }">
                      {{ (pnlResult.summary?.sharpe_ratio ?? 0).toFixed(2) }}
                    </span>
                  </template>
                </n-statistic>
              </n-gi>
              <n-gi span="6 m:1">
                <n-statistic label="Profit Factor">
                  <template #default>
                    {{ pnlResult.summary?.profit_factor != null ? pnlResult.summary.profit_factor.toFixed(2) : '-' }}
                  </template>
                </n-statistic>
              </n-gi>
              <n-gi span="6 m:1">
                <n-statistic label="总收益">
                  <template #default>
                    <span :style="{ color: (pnlResult.summary?.total_return_pct ?? 0) >= 0 ? colors.positive : colors.negative }">
                      {{ (pnlResult.summary?.total_return_pct ?? 0) >= 0 ? '+' : '' }}{{ (pnlResult.summary?.total_return_pct ?? 0).toFixed(2) }}%
                    </span>
                  </template>
                </n-statistic>
              </n-gi>
            </n-grid>

            <!-- 极值 -->
            <n-space style="margin-top: 12px" :size="12">
              <n-tag type="success" size="small">
                最大单笔盈利: +{{ (pnlResult.summary?.max_single_win_pct ?? 0).toFixed(2) }}%
              </n-tag>
              <n-tag type="error" size="small">
                最大单笔亏损: {{ (pnlResult.summary?.max_single_loss_pct ?? 0).toFixed(2) }}%
              </n-tag>
              <n-tag size="small">
                胜 {{ pnlResult.summary?.win_count ?? 0 }} / 负 {{ pnlResult.summary?.loss_count ?? 0 }}
              </n-tag>
              <n-tag v-if="pnlResult.summary?.mtm_trades" size="small" type="warning">
                MTM: {{ pnlResult.summary.mtm_trades }}
              </n-tag>
            </n-space>

            <!-- Benchmark 对比 -->
            <div v-if="pnlResult.summary?.benchmark_avg_pct != null" style="margin-top: 12px; padding: 10px 12px; background: rgba(64,158,255,0.04); border-radius: 6px; border: 1px solid rgba(64,158,255,0.12)">
              <span :style="{ fontSize: '13px', color: colors.textSecondary }">vs 同期买入持有 (Benchmark):</span>
              <n-space style="margin-top: 6px" :size="16">
                <span style="font-size: 13px">
                  策略均收益
                  <span style="font-weight: 600" :style="{ color: (pnlResult.summary?.avg_return_pct ?? 0) >= 0 ? colors.positive : colors.negative }">
                    {{ (pnlResult.summary?.avg_return_pct ?? 0) >= 0 ? '+' : '' }}{{ (pnlResult.summary?.avg_return_pct ?? 0).toFixed(2) }}%
                  </span>
                </span>
                <span style="font-size: 13px">
                  Benchmark 均收益
                  <span style="font-weight: 600" :style="{ color: (pnlResult.summary?.benchmark_avg_pct ?? 0) >= 0 ? colors.positive : colors.negative }">
                    {{ (pnlResult.summary?.benchmark_avg_pct ?? 0) >= 0 ? '+' : '' }}{{ (pnlResult.summary?.benchmark_avg_pct ?? 0).toFixed(2) }}%
                  </span>
                </span>
                <span style="font-size: 13px">
                  Alpha
                  <span style="font-weight: 700" :style="{ color: (pnlResult.summary?.alpha_avg_pct ?? 0) >= 0 ? colors.positive : colors.negative }">
                    {{ (pnlResult.summary?.alpha_avg_pct ?? 0) >= 0 ? '+' : '' }}{{ (pnlResult.summary?.alpha_avg_pct ?? 0).toFixed(2) }}%
                  </span>
                </span>
                <span style="font-size: 13px">
                  策略胜率 <span style="font-weight: 600">{{ ((pnlResult.summary?.win_rate ?? 0) * 100).toFixed(1) }}%</span>
                  vs Benchmark 胜率 <span style="font-weight: 600">{{ ((pnlResult.summary?.benchmark_win_rate ?? 0) * 100).toFixed(1) }}%</span>
                </span>
              </n-space>
            </div>
          </n-card>

          <!-- 分组统计 -->
          <n-grid :cols="3" :x-gap="16" responsive="screen" item-responsive>
            <!-- 按方向 -->
            <n-gi span="3 m:1" v-if="pnlResult.summary?.by_direction">
              <n-card size="small" title="按方向">
                <n-descriptions :column="1" label-placement="left" size="small" bordered>
                  <template v-for="(stats, dir) in pnlResult.summary.by_direction" :key="dir">
                    <n-descriptions-item :label="dir === 'bullish' ? '看多' : '看空'">
                      {{ stats.count }}笔,
                      胜率 {{ (stats.win_rate * 100).toFixed(1) }}%,
                      均收益
                      <span :style="{ color: stats.avg_return >= 0 ? colors.positive : colors.negative }">
                        {{ stats.avg_return >= 0 ? '+' : '' }}{{ stats.avg_return.toFixed(2) }}%
                      </span>
                    </n-descriptions-item>
                  </template>
                </n-descriptions>
              </n-card>
            </n-gi>

            <!-- 按告警类型 -->
            <n-gi span="3 m:1" v-if="pnlResult.summary?.by_alert_type">
              <n-card size="small" title="按告警类型">
                <n-descriptions :column="1" label-placement="left" size="small" bordered>
                  <template v-for="(stats, type) in pnlResult.summary.by_alert_type" :key="type">
                    <n-descriptions-item :label="alertTypeLabel(type as string)">
                      {{ stats.count }}笔,
                      胜率 {{ (stats.win_rate * 100).toFixed(1) }}%,
                      均收益
                      <span :style="{ color: stats.avg_return >= 0 ? colors.positive : colors.negative }">
                        {{ stats.avg_return >= 0 ? '+' : '' }}{{ stats.avg_return.toFixed(2) }}%
                      </span>
                    </n-descriptions-item>
                  </template>
                </n-descriptions>
              </n-card>
            </n-gi>

            <!-- 按置信度 -->
            <n-gi span="3 m:1" v-if="pnlResult.summary?.by_confidence_tier">
              <n-card size="small" title="按置信度">
                <n-descriptions :column="1" label-placement="left" size="small" bordered>
                  <template v-for="(stats, tier) in pnlResult.summary.by_confidence_tier" :key="tier">
                    <n-descriptions-item :label="(tier as string)">
                      {{ stats.count }}笔,
                      胜率 {{ (stats.win_rate * 100).toFixed(1) }}%,
                      均收益
                      <span :style="{ color: stats.avg_return >= 0 ? colors.positive : colors.negative }">
                        {{ stats.avg_return >= 0 ? '+' : '' }}{{ stats.avg_return.toFixed(2) }}%
                      </span>
                    </n-descriptions-item>
                  </template>
                </n-descriptions>
              </n-card>
            </n-gi>
          </n-grid>

          <!-- Ticker 统计 -->
          <n-card v-if="pnlResult.summary?.ticker_stats?.length" size="small" title="Ticker 表现">
            <n-space vertical :size="4">
              <div
                v-for="t in pnlResult.summary.ticker_stats"
                :key="t.ticker"
                style="display: flex; align-items: center; gap: 8px; padding: 4px 0"
              >
                <n-tag size="small" type="info" round style="min-width: 56px; text-align: center">{{ t.ticker }}</n-tag>
                <span :style="{ color: colors.textSecondary, fontSize: '12px', minWidth: '40px' }">{{ t.count }}笔</span>
                <span style="font-size: 12px; min-width: 60px">
                  胜率 {{ (t.win_rate * 100).toFixed(0) }}%
                </span>
                <span
                  style="font-size: 12px; font-weight: 600; min-width: 65px"
                  :style="{ color: t.avg_return >= 0 ? colors.positive : colors.negative }"
                >
                  {{ t.avg_return >= 0 ? '+' : '' }}{{ t.avg_return.toFixed(2) }}%
                </span>
                <n-progress
                  type="line"
                  :percentage="Math.round(t.win_rate * 100)"
                  :show-indicator="false"
                  style="flex: 1"
                  :color="t.win_rate >= 0.5 ? colors.positive : colors.negative"
                />
              </div>
            </n-space>
          </n-card>

          <!-- Top Winners / Losers -->
          <n-grid :cols="2" :x-gap="16" responsive="screen" item-responsive>
            <n-gi span="2 m:1" v-if="pnlResult.summary?.top_winners?.length">
              <n-card size="small" title="Top Winners">
                <n-space vertical :size="4">
                  <div
                    v-for="(t, i) in pnlResult.summary.top_winners"
                    :key="i"
                    style="display: flex; align-items: center; gap: 8px; padding: 4px 0; font-size: 13px"
                  >
                    <n-tag size="small" type="success" round>{{ t.ticker }}</n-tag>
                    <span :style="{ color: colors.positive, fontWeight: 600 }">+{{ t.return_pct.toFixed(2) }}%</span>
                    <span :style="{ color: colors.textTertiary, fontSize: '12px' }">{{ t.entry_date }} → {{ t.exit_date }}</span>
                    <span :style="{ color: colors.textSecondary, fontSize: '12px', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }">
                      {{ t.event_question }}
                    </span>
                  </div>
                </n-space>
              </n-card>
            </n-gi>
            <n-gi span="2 m:1" v-if="pnlResult.summary?.top_losers?.length">
              <n-card size="small" title="Top Losers">
                <n-space vertical :size="4">
                  <div
                    v-for="(t, i) in pnlResult.summary.top_losers"
                    :key="i"
                    style="display: flex; align-items: center; gap: 8px; padding: 4px 0; font-size: 13px"
                  >
                    <n-tag size="small" type="error" round>{{ t.ticker }}</n-tag>
                    <span :style="{ color: colors.negative, fontWeight: 600 }">{{ t.return_pct.toFixed(2) }}%</span>
                    <span :style="{ color: colors.textTertiary, fontSize: '12px' }">{{ t.entry_date }} → {{ t.exit_date }}</span>
                    <span :style="{ color: colors.textSecondary, fontSize: '12px', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }">
                      {{ t.event_question }}
                    </span>
                  </div>
                </n-space>
              </n-card>
            </n-gi>
          </n-grid>

          <!-- 交易明细表 -->
          <n-card size="small" title="交易明细" :segmented="{ content: true }">
            <n-data-table
              :columns="pnlTradeColumns"
              :data="pnlResult.trades || []"
              :row-key="(row: any, idx: number) => idx"
              size="small"
              :max-height="400"
              :scroll-x="1000"
              :row-class-name="(row: any) => row.return_pct >= 0 ? '' : ''"
            />
          </n-card>
        </template>

        <!-- ============================================================ -->
        <!-- A股新闻信号回测 -->
        <!-- ============================================================ -->

        <!-- A股新闻信号回测配置 + 按钮 -->
        <n-card size="small" title="A股新闻信号回测">
          <n-grid :cols="3" :x-gap="12" :y-gap="12">
            <n-gi>
              <n-form-item label="持仓天数" :show-feedback="false">
                <n-input-number v-model:value="cnPnlConfig.holding_days" :min="1" :max="30" :step="1" size="small" />
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item label="最低置信度" :show-feedback="false">
                <n-input-number v-model:value="cnPnlConfig.min_confidence" :min="0" :max="1" :step="0.1" size="small" />
              </n-form-item>
            </n-gi>
            <n-gi style="display: flex; align-items: flex-end">
              <n-button
                type="primary"
                :loading="cnPnlRunLoading"
                @click="doRunCnPnl"
              >
                运行 A 股新闻信号回测
              </n-button>
            </n-gi>
          </n-grid>
        </n-card>

        <!-- A股新闻信号回测进度 -->
        <n-card v-if="cnPnlTaskId && cnPnlTask && cnPnlTask.status === 'running'" size="small" title="A股新闻信号回测进度">
          <n-progress type="line" :percentage="cnPnlTask.progress" :indicator-placement="'inside'" />
          <div :style="{ marginTop: '8px', color: colors.textTertiary, fontSize: '13px' }">{{ cnPnlTask.message }}</div>
        </n-card>

        <!-- A股 P&L 结果 -->
        <template v-if="cnPnlResult">
          <!-- 汇总统计 -->
          <n-card size="small" title="A股新闻信号回测汇总">
            <n-grid :cols="6" :x-gap="12" :y-gap="12" responsive="screen" item-responsive>
              <n-gi span="6 m:1">
                <n-statistic label="总交易数" :value="cnPnlResult.summary?.total_trades ?? 0" />
              </n-gi>
              <n-gi span="6 m:1">
                <n-statistic label="胜率">
                  <template #default>
                    <span :style="{ color: (cnPnlResult.summary?.win_rate ?? 0) >= 0.5 ? colors.positive : colors.negative }">
                      {{ ((cnPnlResult.summary?.win_rate ?? 0) * 100).toFixed(1) }}%
                    </span>
                  </template>
                </n-statistic>
              </n-gi>
              <n-gi span="6 m:1">
                <n-statistic label="平均收益">
                  <template #default>
                    <span :style="{ color: (cnPnlResult.summary?.avg_return_pct ?? 0) >= 0 ? colors.positive : colors.negative }">
                      {{ (cnPnlResult.summary?.avg_return_pct ?? 0) >= 0 ? '+' : '' }}{{ (cnPnlResult.summary?.avg_return_pct ?? 0).toFixed(2) }}%
                    </span>
                  </template>
                </n-statistic>
              </n-gi>
              <n-gi span="6 m:1">
                <n-statistic label="Sharpe">
                  <template #default>
                    <span :style="{ color: (cnPnlResult.summary?.sharpe_ratio ?? 0) >= 0 ? colors.positive : colors.negative }">
                      {{ (cnPnlResult.summary?.sharpe_ratio ?? 0).toFixed(2) }}
                    </span>
                  </template>
                </n-statistic>
              </n-gi>
              <n-gi span="6 m:1">
                <n-statistic label="Profit Factor">
                  <template #default>
                    {{ cnPnlResult.summary?.profit_factor != null ? cnPnlResult.summary.profit_factor.toFixed(2) : '-' }}
                  </template>
                </n-statistic>
              </n-gi>
              <n-gi span="6 m:1">
                <n-statistic label="总收益">
                  <template #default>
                    <span :style="{ color: (cnPnlResult.summary?.total_return_pct ?? 0) >= 0 ? colors.positive : colors.negative }">
                      {{ (cnPnlResult.summary?.total_return_pct ?? 0) >= 0 ? '+' : '' }}{{ (cnPnlResult.summary?.total_return_pct ?? 0).toFixed(2) }}%
                    </span>
                  </template>
                </n-statistic>
              </n-gi>
            </n-grid>

            <n-space style="margin-top: 12px" :size="12">
              <n-tag type="success" size="small">
                最大单笔盈利: +{{ (cnPnlResult.summary?.max_single_win_pct ?? 0).toFixed(2) }}%
              </n-tag>
              <n-tag type="error" size="small">
                最大单笔亏损: {{ (cnPnlResult.summary?.max_single_loss_pct ?? 0).toFixed(2) }}%
              </n-tag>
              <n-tag size="small">
                胜 {{ cnPnlResult.summary?.win_count ?? 0 }} / 负 {{ cnPnlResult.summary?.loss_count ?? 0 }}
              </n-tag>
              <n-tag v-if="cnPnlResult.summary?.mtm_trades" size="small" type="warning">
                MTM: {{ cnPnlResult.summary.mtm_trades }}
              </n-tag>
            </n-space>

            <!-- Benchmark 对比 -->
            <div v-if="cnPnlResult.summary?.benchmark_avg_pct != null" style="margin-top: 12px; padding: 10px 12px; background: rgba(64,158,255,0.04); border-radius: 6px; border: 1px solid rgba(64,158,255,0.12)">
              <span :style="{ fontSize: '13px', color: colors.textSecondary }">vs 同期买入持有 (Benchmark):</span>
              <n-space style="margin-top: 6px" :size="16">
                <span style="font-size: 13px">
                  策略均收益
                  <span style="font-weight: 600" :style="{ color: (cnPnlResult.summary?.avg_return_pct ?? 0) >= 0 ? colors.positive : colors.negative }">
                    {{ (cnPnlResult.summary?.avg_return_pct ?? 0) >= 0 ? '+' : '' }}{{ (cnPnlResult.summary?.avg_return_pct ?? 0).toFixed(2) }}%
                  </span>
                </span>
                <span style="font-size: 13px">
                  Benchmark 均收益
                  <span style="font-weight: 600" :style="{ color: (cnPnlResult.summary?.benchmark_avg_pct ?? 0) >= 0 ? colors.positive : colors.negative }">
                    {{ (cnPnlResult.summary?.benchmark_avg_pct ?? 0) >= 0 ? '+' : '' }}{{ (cnPnlResult.summary?.benchmark_avg_pct ?? 0).toFixed(2) }}%
                  </span>
                </span>
                <span style="font-size: 13px">
                  Alpha
                  <span style="font-weight: 700" :style="{ color: (cnPnlResult.summary?.alpha_avg_pct ?? 0) >= 0 ? colors.positive : colors.negative }">
                    {{ (cnPnlResult.summary?.alpha_avg_pct ?? 0) >= 0 ? '+' : '' }}{{ (cnPnlResult.summary?.alpha_avg_pct ?? 0).toFixed(2) }}%
                  </span>
                </span>
                <span style="font-size: 13px">
                  策略胜率 <span style="font-weight: 600">{{ ((cnPnlResult.summary?.win_rate ?? 0) * 100).toFixed(1) }}%</span>
                  vs Benchmark 胜率 <span style="font-weight: 600">{{ ((cnPnlResult.summary?.benchmark_win_rate ?? 0) * 100).toFixed(1) }}%</span>
                </span>
              </n-space>
            </div>
          </n-card>

          <!-- 分组统计 -->
          <n-grid :cols="3" :x-gap="16" responsive="screen" item-responsive>
            <n-gi span="3 m:1" v-if="cnPnlResult.summary?.by_direction">
              <n-card size="small" title="按方向">
                <n-descriptions :column="1" label-placement="left" size="small" bordered>
                  <template v-for="(stats, dir) in cnPnlResult.summary.by_direction" :key="dir">
                    <n-descriptions-item :label="dir === 'bullish' ? '看多' : '看空'">
                      {{ stats.count }}笔,
                      胜率 {{ (stats.win_rate * 100).toFixed(1) }}%,
                      均收益
                      <span :style="{ color: stats.avg_return >= 0 ? colors.positive : colors.negative }">
                        {{ stats.avg_return >= 0 ? '+' : '' }}{{ stats.avg_return.toFixed(2) }}%
                      </span>
                    </n-descriptions-item>
                  </template>
                </n-descriptions>
              </n-card>
            </n-gi>

            <n-gi span="3 m:1" v-if="cnPnlResult.summary?.by_alert_type">
              <n-card size="small" title="按告警类型">
                <n-descriptions :column="1" label-placement="left" size="small" bordered>
                  <template v-for="(stats, type) in cnPnlResult.summary.by_alert_type" :key="type">
                    <n-descriptions-item :label="alertTypeLabel(type as string)">
                      {{ stats.count }}笔,
                      胜率 {{ (stats.win_rate * 100).toFixed(1) }}%,
                      均收益
                      <span :style="{ color: stats.avg_return >= 0 ? colors.positive : colors.negative }">
                        {{ stats.avg_return >= 0 ? '+' : '' }}{{ stats.avg_return.toFixed(2) }}%
                      </span>
                    </n-descriptions-item>
                  </template>
                </n-descriptions>
              </n-card>
            </n-gi>

            <n-gi span="3 m:1" v-if="cnPnlResult.summary?.by_confidence_tier">
              <n-card size="small" title="按置信度">
                <n-descriptions :column="1" label-placement="left" size="small" bordered>
                  <template v-for="(stats, tier) in cnPnlResult.summary.by_confidence_tier" :key="tier">
                    <n-descriptions-item :label="(tier as string)">
                      {{ stats.count }}笔,
                      胜率 {{ (stats.win_rate * 100).toFixed(1) }}%,
                      均收益
                      <span :style="{ color: stats.avg_return >= 0 ? colors.positive : colors.negative }">
                        {{ stats.avg_return >= 0 ? '+' : '' }}{{ stats.avg_return.toFixed(2) }}%
                      </span>
                    </n-descriptions-item>
                  </template>
                </n-descriptions>
              </n-card>
            </n-gi>
          </n-grid>

          <!-- Ticker 统计 -->
          <n-card v-if="cnPnlResult.summary?.ticker_stats?.length" size="small" title="A股个股表现">
            <n-space vertical :size="4">
              <div
                v-for="t in cnPnlResult.summary.ticker_stats"
                :key="t.ticker"
                style="display: flex; align-items: center; gap: 8px; padding: 4px 0"
              >
                <n-tag size="small" type="warning" round style="min-width: 56px; text-align: center">{{ t.name || t.ticker }}</n-tag>
                <span :style="{ color: colors.textTertiary, fontSize: '11px', minWidth: '75px' }">{{ t.ticker }}</span>
                <span :style="{ color: colors.textSecondary, fontSize: '12px', minWidth: '40px' }">{{ t.count }}笔</span>
                <span style="font-size: 12px; min-width: 60px">
                  胜率 {{ (t.win_rate * 100).toFixed(0) }}%
                </span>
                <span
                  style="font-size: 12px; font-weight: 600; min-width: 65px"
                  :style="{ color: t.avg_return >= 0 ? colors.positive : colors.negative }"
                >
                  {{ t.avg_return >= 0 ? '+' : '' }}{{ t.avg_return.toFixed(2) }}%
                </span>
                <n-progress
                  type="line"
                  :percentage="Math.round(t.win_rate * 100)"
                  :show-indicator="false"
                  style="flex: 1"
                  :color="t.win_rate >= 0.5 ? colors.positive : colors.negative"
                />
              </div>
            </n-space>
          </n-card>

          <!-- Top Winners / Losers -->
          <n-grid :cols="2" :x-gap="16" responsive="screen" item-responsive>
            <n-gi span="2 m:1" v-if="cnPnlResult.summary?.top_winners?.length">
              <n-card size="small" title="Top Winners">
                <n-space vertical :size="4">
                  <div
                    v-for="(t, i) in cnPnlResult.summary.top_winners"
                    :key="i"
                    style="display: flex; align-items: center; gap: 8px; padding: 4px 0; font-size: 13px"
                  >
                    <n-tag size="small" type="success" round>{{ t.name || t.ticker }}</n-tag>
                    <span :style="{ color: colors.positive, fontWeight: 600 }">+{{ t.return_pct.toFixed(2) }}%</span>
                    <span :style="{ color: colors.textTertiary, fontSize: '12px' }">{{ t.entry_date }} → {{ t.exit_date }}</span>
                    <span :style="{ color: colors.textSecondary, fontSize: '12px', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }">
                      {{ t.event_question }}
                    </span>
                  </div>
                </n-space>
              </n-card>
            </n-gi>
            <n-gi span="2 m:1" v-if="cnPnlResult.summary?.top_losers?.length">
              <n-card size="small" title="Top Losers">
                <n-space vertical :size="4">
                  <div
                    v-for="(t, i) in cnPnlResult.summary.top_losers"
                    :key="i"
                    style="display: flex; align-items: center; gap: 8px; padding: 4px 0; font-size: 13px"
                  >
                    <n-tag size="small" type="error" round>{{ t.name || t.ticker }}</n-tag>
                    <span :style="{ color: colors.negative, fontWeight: 600 }">{{ t.return_pct.toFixed(2) }}%</span>
                    <span :style="{ color: colors.textTertiary, fontSize: '12px' }">{{ t.entry_date }} → {{ t.exit_date }}</span>
                    <span :style="{ color: colors.textSecondary, fontSize: '12px', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }">
                      {{ t.event_question }}
                    </span>
                  </div>
                </n-space>
              </n-card>
            </n-gi>
          </n-grid>

          <!-- 交易明细表 -->
          <n-card size="small" title="A股交易明细" :segmented="{ content: true }">
            <n-data-table
              :columns="cnPnlTradeColumns"
              :data="cnPnlResult.trades || []"
              :row-key="(row: any, idx: number) => idx"
              size="small"
              :max-height="400"
              :scroll-x="1100"
            />
          </n-card>
        </template>
      </n-space>
    </n-tab-pane>

    <!-- ============================================================ -->
    <!-- 历史影响 Tab -->
    <!-- ============================================================ -->
    <n-tab-pane name="impact" tab="历史影响">
      <n-spin :show="impactLoading">
        <n-space vertical :size="16">
          <!-- 筛选条件 -->
          <n-card size="small">
            <n-space align="center" :size="16">
              <n-form-item label="回看天数" :show-feedback="false" label-placement="left">
                <n-input-number v-model:value="impactDays" :min="7" :max="1095" :step="30" size="small" style="width: 120px" />
              </n-form-item>
              <n-form-item label="最低置信度" :show-feedback="false" label-placement="left">
                <n-input-number v-model:value="impactMinConf" :min="0" :max="1" :step="0.1" size="small" style="width: 100px" />
              </n-form-item>
              <n-button type="primary" size="small" @click="loadImpact" :loading="impactLoading">
                刷新
              </n-button>
            </n-space>
          </n-card>

          <template v-if="impactData">
            <!-- 汇总统计 -->
            <n-card size="small" title="影响概览">
              <n-grid :cols="6" :x-gap="12" :y-gap="12" responsive="screen" item-responsive>
                <n-gi span="6 m:1">
                  <n-statistic label="总告警数" :value="impactData.summary?.total_alerts ?? 0" />
                </n-gi>
                <n-gi span="6 m:1">
                  <n-statistic label="LLM 分析数" :value="impactData.summary?.alerts_with_llm ?? 0" />
                </n-gi>
                <n-gi span="6 m:1">
                  <n-statistic label="受影响行业" :value="impactData.industry_impact?.length ?? 0" />
                </n-gi>
                <n-gi span="6 m:1">
                  <n-statistic label="受影响A股" :value="impactData.stock_impact?.length ?? 0" />
                </n-gi>
                <n-gi span="6 m:1">
                  <n-statistic label="已桥接文章" :value="impactData.summary?.bridged_articles ?? 0" />
                </n-gi>
                <n-gi span="6 m:1">
                  <n-statistic label="已桥接分析" :value="impactData.summary?.bridged_analysis ?? 0" />
                </n-gi>
              </n-grid>

              <n-space style="margin-top: 12px" :size="8">
                <template v-if="impactData.summary?.avg_sentiment != null">
                  <span :style="{ color: colors.textTertiary, fontSize: '13px' }">平均情感:</span>
                  <n-tag :type="impactData.summary.avg_sentiment >= 0 ? 'success' : 'error'" size="small">
                    {{ impactData.summary.avg_sentiment.toFixed(3) }}
                  </n-tag>
                </template>
                <template v-if="impactData.summary?.avg_confidence != null">
                  <span :style="{ color: colors.textTertiary, fontSize: '13px' }">平均置信度:</span>
                  <n-tag size="small">{{ impactData.summary.avg_confidence.toFixed(3) }}</n-tag>
                </template>
                <template v-if="impactData.summary?.date_range">
                  <span :style="{ color: colors.textTertiary, fontSize: '13px' }">时间范围:</span>
                  <span style="font-size: 13px">
                    {{ impactData.summary.date_range.earliest }} ~ {{ impactData.summary.date_range.latest }}
                  </span>
                </template>
              </n-space>
            </n-card>

            <!-- 分布卡片 -->
            <n-grid :cols="2" :x-gap="16" responsive="screen" item-responsive>
              <!-- 事件分类分布 -->
              <n-gi span="2 m:1" v-if="impactData.category_distribution && Object.keys(impactData.category_distribution).length">
                <n-card size="small" title="事件分类分布">
                  <n-space vertical :size="6">
                    <div
                      v-for="(count, cat) in impactData.category_distribution"
                      :key="cat as string"
                      style="display: flex; align-items: center; gap: 8px; padding: 4px 0"
                    >
                      <span style="min-width: 80px; font-size: 13px">{{ cat }}</span>
                      <n-progress
                        type="line"
                        :percentage="Math.round((count as number / impactData.summary.total_alerts) * 100)"
                        :show-indicator="false"
                        style="flex: 1"
                        :color="colors.primary"
                      />
                      <span :style="{ color: colors.textSecondary, fontSize: '12px', minWidth: '40px', textAlign: 'right' }">{{ count }}</span>
                    </div>
                  </n-space>
                </n-card>
              </n-gi>

              <!-- 告警类型分布 -->
              <n-gi span="2 m:1" v-if="impactData.alert_type_distribution && Object.keys(impactData.alert_type_distribution).length">
                <n-card size="small" title="告警类型分布">
                  <n-space vertical :size="6">
                    <div
                      v-for="(count, type) in impactData.alert_type_distribution"
                      :key="type as string"
                      style="display: flex; align-items: center; gap: 8px; padding: 4px 0"
                    >
                      <span style="min-width: 80px; font-size: 13px">{{ alertTypeLabel(type as string) }}</span>
                      <n-progress
                        type="line"
                        :percentage="Math.round((count as number / impactData.summary.total_alerts) * 100)"
                        :show-indicator="false"
                        style="flex: 1"
                        :color="colors.warning"
                      />
                      <span :style="{ color: colors.textSecondary, fontSize: '12px', minWidth: '40px', textAlign: 'right' }">{{ count }}</span>
                    </div>
                  </n-space>
                </n-card>
              </n-gi>
            </n-grid>

            <!-- 行业影响 & 股票影响 -->
            <n-grid :cols="2" :x-gap="16" responsive="screen" item-responsive>
              <!-- 行业影响 -->
              <n-gi span="2 m:1" v-if="impactData.industry_impact?.length">
                <n-card size="small" title="受影响申万行业">
                  <div style="max-height: 500px; overflow-y: auto">
                    <table style="width: 100%; border-collapse: collapse; font-size: 13px">
                      <thead>
                        <tr :style="{ borderBottom: `1px solid ${colors.borderLight}`, color: colors.textTertiary, fontSize: '12px' }">
                          <th style="text-align: left; padding: 6px 8px">行业</th>
                          <th style="text-align: right; padding: 6px 8px">命中次数</th>
                          <th style="text-align: right; padding: 6px 8px">平均情感</th>
                          <th style="text-align: right; padding: 6px 8px">平均置信度</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr
                          v-for="ind in impactData.industry_impact"
                          :key="ind.industry"
                          :style="{ backgroundColor: sentimentBg(ind.avg_sentiment), borderBottom: `1px solid ${colors.borderSubtle}` }"
                        >
                          <td style="padding: 6px 8px; font-weight: 500">{{ ind.industry }}</td>
                          <td style="padding: 6px 8px; text-align: right">{{ ind.count }}</td>
                          <td style="padding: 6px 8px; text-align: right; font-weight: 600" :style="{ color: sentimentColor(ind.avg_sentiment) }">
                            {{ ind.avg_sentiment != null ? ind.avg_sentiment.toFixed(3) : '-' }}
                          </td>
                          <td :style="{ padding: '6px 8px', textAlign: 'right', color: colors.textSecondary }">
                            {{ ind.avg_confidence != null ? ind.avg_confidence.toFixed(2) : '-' }}
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </n-card>
              </n-gi>

              <!-- 股票影响 -->
              <n-gi span="2 m:1" v-if="impactData.stock_impact?.length">
                <n-card size="small" title="受影响A股">
                  <div style="max-height: 500px; overflow-y: auto">
                    <table style="width: 100%; border-collapse: collapse; font-size: 13px">
                      <thead>
                        <tr :style="{ borderBottom: `1px solid ${colors.borderLight}`, color: colors.textTertiary, fontSize: '12px' }">
                          <th style="text-align: left; padding: 6px 8px">股票</th>
                          <th style="text-align: left; padding: 6px 4px">代码</th>
                          <th style="text-align: right; padding: 6px 8px">次数</th>
                          <th style="text-align: right; padding: 6px 8px">情感</th>
                          <th style="text-align: center; padding: 6px 8px">利好/利空</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr
                          v-for="s in impactData.stock_impact"
                          :key="s.code"
                          :style="{ backgroundColor: sentimentBg(s.avg_sentiment), borderBottom: `1px solid ${colors.borderSubtle}` }"
                        >
                          <td style="padding: 6px 8px; font-weight: 500">{{ s.name }}</td>
                          <td :style="{ padding: '6px 4px', color: colors.textTertiary, fontSize: '12px' }">{{ s.code }}</td>
                          <td style="padding: 6px 8px; text-align: right">{{ s.count }}</td>
                          <td style="padding: 6px 8px; text-align: right; font-weight: 600" :style="{ color: sentimentColor(s.avg_sentiment) }">
                            {{ s.avg_sentiment != null ? s.avg_sentiment.toFixed(3) : '-' }}
                          </td>
                          <td style="padding: 6px 8px; text-align: center">
                            <span v-if="s.bullish" :style="{ color: colors.positive, fontSize: '12px' }">{{ s.bullish }}↑</span>
                            <span v-if="s.bullish && s.bearish" style="color: #ccc; margin: 0 2px">/</span>
                            <span v-if="s.bearish" :style="{ color: colors.negative, fontSize: '12px' }">{{ s.bearish }}↓</span>
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </n-card>
              </n-gi>
            </n-grid>

            <!-- 每日时间线 -->
            <n-card v-if="impactData.daily_timeline?.length" size="small" title="每日告警时间线">
              <div style="max-height: 300px; overflow-y: auto">
                <div style="display: flex; align-items: flex-end; gap: 2px; min-height: 120px; padding: 8px 0">
                  <div
                    v-for="d in impactData.daily_timeline"
                    :key="d.date"
                    :title="`${d.date}: ${d.count} 条告警, 情感 ${d.avg_sentiment?.toFixed(2) ?? '-'}`"
                    :style="{
                      flex: 1,
                      minWidth: '4px',
                      maxWidth: '16px',
                      height: Math.max(4, d.count * 12) + 'px',
                      backgroundColor: sentimentColor(d.avg_sentiment),
                      borderRadius: '2px 2px 0 0',
                      opacity: 0.7,
                      cursor: 'pointer',
                    }"
                  />
                </div>
                <div :style="{ display: 'flex', justifyContent: 'space-between', fontSize: '11px', color: colors.textTertiary, padding: '0 2px' }">
                  <span>{{ impactData.daily_timeline[0]?.date }}</span>
                  <span>{{ impactData.daily_timeline[impactData.daily_timeline.length - 1]?.date }}</span>
                </div>
              </div>
            </n-card>

            <!-- 最近告警列表 -->
            <n-card v-if="impactData.recent_alerts?.length" size="small" title="最近告警" :segmented="{ content: true }">
              <n-data-table
                :columns="impactAlertColumns"
                :data="impactData.recent_alerts"
                :row-key="(row: any) => row.id"
                size="small"
                :max-height="400"
                :scroll-x="900"
                :row-props="(row: any) => ({
                  style: 'cursor: pointer',
                  onClick: () => openImpactAlertDetail(row),
                })"
              />
            </n-card>

            <!-- 空状态 -->
            <n-card v-if="impactData.summary?.total_alerts === 0" size="small">
              <n-empty description="暂无 Polymarket 历史告警数据">
                <template #extra>
                  <span :style="{ color: colors.textTertiary, fontSize: '13px' }">
                    请先运行「量化回测」或启动「实时监控」生成告警
                  </span>
                </template>
              </n-empty>
            </n-card>
          </template>
        </n-space>
      </n-spin>
    </n-tab-pane>
  </n-tabs>

  <!-- 告警详情抽屉（监控 Tab） -->
  <n-drawer v-model:show="showDetail" :width="520" placement="right">
    <n-drawer-content v-if="selectedAlert" title="告警详情">
      <n-space vertical :size="16">
        <n-card size="small" title="事件信息">
          <n-descriptions :column="1" label-placement="left" size="small">
            <n-descriptions-item label="事件问题">
              {{ selectedAlert.question }}
            </n-descriptions-item>
            <n-descriptions-item label="告警类型">
              <n-tag size="small" :type="selectedAlert.alert_type === 'spike_5m' ? 'error' : selectedAlert.alert_type === 'spike_1h' ? 'warning' : 'info'">
                {{ alertTypeLabel(selectedAlert.alert_type) }}
              </n-tag>
            </n-descriptions-item>
            <n-descriptions-item label="赔率变动">
              {{ formatPrice(selectedAlert.price_before) }} → {{ formatPrice(selectedAlert.price_after) }}
              <n-tag
                size="small"
                :type="selectedAlert.price_change >= 0 ? 'success' : 'error'"
                style="margin-left: 8px"
              >
                {{ formatChange(selectedAlert.price_change) }}
              </n-tag>
            </n-descriptions-item>
            <n-descriptions-item label="触发时间">
              {{ selectedAlert.created_at ? new Date(selectedAlert.created_at).toLocaleString() : '-' }}
            </n-descriptions-item>
          </n-descriptions>
        </n-card>

        <n-card v-if="selectedAlert.llm_summary" size="small" title="LLM 分析">
          <n-space vertical :size="8">
            <div style="line-height: 1.6">{{ selectedAlert.llm_summary }}</div>
            <n-space :size="8">
              <n-tag size="small" :type="(selectedAlert.llm_sentiment ?? 0) >= 0 ? 'success' : 'error'">
                情感: {{ selectedAlert.llm_sentiment?.toFixed(2) ?? '-' }}
              </n-tag>
              <n-tag size="small">
                置信度: {{ selectedAlert.llm_confidence?.toFixed(2) ?? '-' }}
              </n-tag>
            </n-space>
          </n-space>
        </n-card>

        <n-card
          v-if="selectedAlert.affected_tickers && selectedAlert.affected_tickers.length"
          size="small"
          title="受影响美股"
        >
          <n-space vertical :size="6">
            <div
              v-for="(t, i) in selectedAlert.affected_tickers"
              :key="i"
              style="display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid #e8ecf0"
            >
              <n-tag :type="directionTag(t.direction)" size="small" round style="min-width: 56px; text-align: center">
                {{ t.ticker }}
              </n-tag>
              <n-tag size="small" :type="directionTag(t.direction)" :bordered="false">
                {{ directionLabel(t.direction) }}
              </n-tag>
              <span :style="{ color: colors.textTertiary, fontSize: '12px', whiteSpace: 'nowrap' }">
                {{ (t.confidence * 100).toFixed(0) }}%
              </span>
              <span v-if="t.reasoning" :style="{ color: colors.textSecondary, fontSize: '12px', flex: 1, lineHeight: 1.4 }">
                {{ t.reasoning }}
              </span>
            </div>
          </n-space>
        </n-card>

        <n-card
          v-if="selectedAlert.affected_a_shares && selectedAlert.affected_a_shares.length"
          size="small"
          title="受影响A股"
        >
          <n-space vertical :size="6">
            <div
              v-for="(s, i) in selectedAlert.affected_a_shares"
              :key="i"
              style="display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid #e8ecf0"
            >
              <n-tag :type="directionTag(s.direction)" size="small" round style="min-width: 56px; text-align: center">
                {{ s.name }}
              </n-tag>
              <span :style="{ color: colors.textTertiary, fontSize: '12px', whiteSpace: 'nowrap' }">
                {{ s.code }}
              </span>
              <n-tag size="small" :type="directionTag(s.direction)" :bordered="false">
                {{ directionLabel(s.direction) }}
              </n-tag>
              <span :style="{ color: colors.textTertiary, fontSize: '12px', whiteSpace: 'nowrap' }">
                {{ (s.confidence * 100).toFixed(0) }}%
              </span>
              <span v-if="s.reasoning" :style="{ color: colors.textSecondary, fontSize: '12px', flex: 1, lineHeight: 1.4 }">
                {{ s.reasoning }}
              </span>
            </div>
          </n-space>
        </n-card>

        <n-card
          v-if="(selectedAlert.affected_sectors && selectedAlert.affected_sectors.length) || (selectedAlert.affected_sw_industries && selectedAlert.affected_sw_industries.length)"
          size="small"
          title="受影响行业"
        >
          <n-space vertical :size="8">
            <div v-if="selectedAlert.affected_sw_industries && selectedAlert.affected_sw_industries.length">
              <span :style="{ color: colors.textTertiary, fontSize: '12px', marginRight: '8px' }">申万行业:</span>
              <n-tag v-for="s in selectedAlert.affected_sw_industries" :key="s" size="small" style="margin: 2px">
                {{ s }}
              </n-tag>
            </div>
            <div v-if="selectedAlert.affected_sectors && selectedAlert.affected_sectors.length">
              <span :style="{ color: colors.textTertiary, fontSize: '12px', marginRight: '8px' }">GICS 行业:</span>
              <n-tag v-for="s in selectedAlert.affected_sectors" :key="s" size="small" style="margin: 2px">
                {{ s }}
              </n-tag>
            </div>
          </n-space>
        </n-card>
      </n-space>
    </n-drawer-content>
  </n-drawer>

  <!-- 量化回测告警详情抽屉 -->
  <n-drawer v-model:show="btShowAlertDetail" :width="520" placement="right">
    <n-drawer-content v-if="btSelectedAlert" title="量化回测告警详情">
      <n-space vertical :size="16">
        <n-card size="small" title="事件信息">
          <n-descriptions :column="1" label-placement="left" size="small">
            <n-descriptions-item label="事件问题">
              {{ btSelectedAlert.question }}
            </n-descriptions-item>
            <n-descriptions-item label="告警类型">
              <n-tag size="small" :type="btSelectedAlert.alert_type === 'spike_5m' ? 'error' : btSelectedAlert.alert_type === 'spike_1h' ? 'warning' : 'info'">
                {{ alertTypeLabel(btSelectedAlert.alert_type) }}
              </n-tag>
            </n-descriptions-item>
            <n-descriptions-item label="赔率变动">
              {{ formatPrice(btSelectedAlert.price_before) }} → {{ formatPrice(btSelectedAlert.price_after) }}
              <n-tag
                size="small"
                :type="btSelectedAlert.price_change >= 0 ? 'success' : 'error'"
                style="margin-left: 8px"
              >
                {{ formatChange(btSelectedAlert.price_change) }}
              </n-tag>
            </n-descriptions-item>
            <n-descriptions-item label="触发时间">
              {{ btSelectedAlert.timestamp ? new Date(btSelectedAlert.timestamp).toLocaleString() : '-' }}
            </n-descriptions-item>
          </n-descriptions>
        </n-card>

        <n-card v-if="btSelectedAlert.llm_summary" size="small" title="LLM 分析">
          <n-space vertical :size="8">
            <div style="line-height: 1.6">{{ btSelectedAlert.llm_summary }}</div>
            <n-space :size="8">
              <n-tag size="small" :type="(btSelectedAlert.llm_sentiment ?? 0) >= 0 ? 'success' : 'error'">
                情感: {{ btSelectedAlert.llm_sentiment?.toFixed(2) ?? '-' }}
              </n-tag>
              <n-tag size="small">
                置信度: {{ btSelectedAlert.llm_confidence?.toFixed(2) ?? '-' }}
              </n-tag>
            </n-space>
          </n-space>
        </n-card>

        <n-card
          v-if="btSelectedAlert.affected_tickers && btSelectedAlert.affected_tickers.length"
          size="small"
          title="受影响美股"
        >
          <n-space vertical :size="6">
            <div
              v-for="(t, i) in btSelectedAlert.affected_tickers"
              :key="i"
              style="display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid #e8ecf0"
            >
              <n-tag :type="directionTag(t.direction)" size="small" round style="min-width: 56px; text-align: center">
                {{ t.ticker }}
              </n-tag>
              <n-tag size="small" :type="directionTag(t.direction)" :bordered="false">
                {{ directionLabel(t.direction) }}
              </n-tag>
              <span :style="{ color: colors.textTertiary, fontSize: '12px', whiteSpace: 'nowrap' }">
                {{ (t.confidence * 100).toFixed(0) }}%
              </span>
              <span v-if="t.reasoning" :style="{ color: colors.textSecondary, fontSize: '12px', flex: 1, lineHeight: 1.4 }">
                {{ t.reasoning }}
              </span>
            </div>
          </n-space>
        </n-card>

        <n-card
          v-if="btSelectedAlert.affected_a_shares && btSelectedAlert.affected_a_shares.length"
          size="small"
          title="受影响A股"
        >
          <n-space vertical :size="6">
            <div
              v-for="(s, i) in btSelectedAlert.affected_a_shares"
              :key="i"
              style="display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid #e8ecf0"
            >
              <n-tag :type="directionTag(s.direction)" size="small" round style="min-width: 56px; text-align: center">
                {{ s.name }}
              </n-tag>
              <span :style="{ color: colors.textTertiary, fontSize: '12px', whiteSpace: 'nowrap' }">
                {{ s.code }}
              </span>
              <n-tag size="small" :type="directionTag(s.direction)" :bordered="false">
                {{ directionLabel(s.direction) }}
              </n-tag>
              <span :style="{ color: colors.textTertiary, fontSize: '12px', whiteSpace: 'nowrap' }">
                {{ (s.confidence * 100).toFixed(0) }}%
              </span>
              <span v-if="s.reasoning" :style="{ color: colors.textSecondary, fontSize: '12px', flex: 1, lineHeight: 1.4 }">
                {{ s.reasoning }}
              </span>
            </div>
          </n-space>
        </n-card>

        <n-card
          v-if="(btSelectedAlert.affected_sectors && btSelectedAlert.affected_sectors.length) || (btSelectedAlert.affected_sw_industries && btSelectedAlert.affected_sw_industries.length)"
          size="small"
          title="受影响行业"
        >
          <n-space vertical :size="8">
            <div v-if="btSelectedAlert.affected_sw_industries && btSelectedAlert.affected_sw_industries.length">
              <span :style="{ color: colors.textTertiary, fontSize: '12px', marginRight: '8px' }">申万行业:</span>
              <n-tag v-for="s in btSelectedAlert.affected_sw_industries" :key="s" size="small" style="margin: 2px">
                {{ s }}
              </n-tag>
            </div>
            <div v-if="btSelectedAlert.affected_sectors && btSelectedAlert.affected_sectors.length">
              <span :style="{ color: colors.textTertiary, fontSize: '12px', marginRight: '8px' }">GICS 行业:</span>
              <n-tag v-for="s in btSelectedAlert.affected_sectors" :key="s" size="small" style="margin: 2px">
                {{ s }}
              </n-tag>
            </div>
          </n-space>
        </n-card>
      </n-space>
    </n-drawer-content>
  </n-drawer>

  <!-- 历史影响告警详情抽屉 -->
  <n-drawer v-model:show="impactShowDetail" :width="520" placement="right">
    <n-drawer-content v-if="impactSelectedAlert" title="告警详情">
      <n-space vertical :size="16">
        <n-card size="small" title="事件信息">
          <n-descriptions :column="1" label-placement="left" size="small">
            <n-descriptions-item label="事件问题">
              {{ impactSelectedAlert.question }}
            </n-descriptions-item>
            <n-descriptions-item label="告警类型">
              <n-tag size="small" :type="impactSelectedAlert.alert_type === 'spike_5m' ? 'error' : impactSelectedAlert.alert_type === 'spike_1h' ? 'warning' : 'info'">
                {{ alertTypeLabel(impactSelectedAlert.alert_type) }}
              </n-tag>
            </n-descriptions-item>
            <n-descriptions-item label="分类">
              {{ impactSelectedAlert.category }}
            </n-descriptions-item>
            <n-descriptions-item label="赔率变动">
              {{ formatPrice(impactSelectedAlert.price_before) }} → {{ formatPrice(impactSelectedAlert.price_after) }}
              <n-tag
                size="small"
                :type="impactSelectedAlert.price_change >= 0 ? 'success' : 'error'"
                style="margin-left: 8px"
              >
                {{ formatChange(impactSelectedAlert.price_change) }}
              </n-tag>
            </n-descriptions-item>
            <n-descriptions-item label="触发时间">
              {{ impactSelectedAlert.created_at ? new Date(impactSelectedAlert.created_at).toLocaleString() : '-' }}
            </n-descriptions-item>
          </n-descriptions>
        </n-card>

        <n-card v-if="impactSelectedAlert.llm_summary" size="small" title="LLM 分析">
          <n-space vertical :size="8">
            <div style="line-height: 1.6">{{ impactSelectedAlert.llm_summary }}</div>
            <n-space :size="8">
              <n-tag size="small" :type="(impactSelectedAlert.llm_sentiment ?? 0) >= 0 ? 'success' : 'error'">
                情感: {{ impactSelectedAlert.llm_sentiment?.toFixed(2) ?? '-' }}
              </n-tag>
              <n-tag size="small">
                置信度: {{ impactSelectedAlert.llm_confidence?.toFixed(2) ?? '-' }}
              </n-tag>
            </n-space>
          </n-space>
        </n-card>

        <n-card
          v-if="impactSelectedAlert.affected_tickers && impactSelectedAlert.affected_tickers.length"
          size="small"
          title="受影响美股"
        >
          <n-space vertical :size="6">
            <div
              v-for="(t, i) in impactSelectedAlert.affected_tickers"
              :key="i"
              style="display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid #e8ecf0"
            >
              <n-tag :type="directionTag(t.direction)" size="small" round style="min-width: 56px; text-align: center">
                {{ t.ticker }}
              </n-tag>
              <n-tag size="small" :type="directionTag(t.direction)" :bordered="false">
                {{ directionLabel(t.direction) }}
              </n-tag>
              <span :style="{ color: colors.textTertiary, fontSize: '12px', whiteSpace: 'nowrap' }">
                {{ (t.confidence * 100).toFixed(0) }}%
              </span>
              <span v-if="t.reasoning" :style="{ color: colors.textSecondary, fontSize: '12px', flex: 1, lineHeight: 1.4 }">
                {{ t.reasoning }}
              </span>
            </div>
          </n-space>
        </n-card>

        <n-card
          v-if="impactSelectedAlert.affected_a_shares && impactSelectedAlert.affected_a_shares.length"
          size="small"
          title="受影响A股"
        >
          <n-space vertical :size="6">
            <div
              v-for="(s, i) in impactSelectedAlert.affected_a_shares"
              :key="i"
              style="display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid #e8ecf0"
            >
              <n-tag :type="directionTag(s.direction)" size="small" round style="min-width: 56px; text-align: center">
                {{ s.name }}
              </n-tag>
              <span :style="{ color: colors.textTertiary, fontSize: '12px', whiteSpace: 'nowrap' }">
                {{ s.code }}
              </span>
              <n-tag size="small" :type="directionTag(s.direction)" :bordered="false">
                {{ directionLabel(s.direction) }}
              </n-tag>
              <span :style="{ color: colors.textTertiary, fontSize: '12px', whiteSpace: 'nowrap' }">
                {{ (s.confidence * 100).toFixed(0) }}%
              </span>
            </div>
          </n-space>
        </n-card>

        <n-card
          v-if="(impactSelectedAlert.affected_sectors && impactSelectedAlert.affected_sectors.length) || (impactSelectedAlert.affected_sw_industries && impactSelectedAlert.affected_sw_industries.length)"
          size="small"
          title="受影响行业"
        >
          <n-space vertical :size="8">
            <div v-if="impactSelectedAlert.affected_sw_industries && impactSelectedAlert.affected_sw_industries.length">
              <span :style="{ color: colors.textTertiary, fontSize: '12px', marginRight: '8px' }">申万行业:</span>
              <n-tag v-for="s in impactSelectedAlert.affected_sw_industries" :key="s" size="small" style="margin: 2px">
                {{ s }}
              </n-tag>
            </div>
            <div v-if="impactSelectedAlert.affected_sectors && impactSelectedAlert.affected_sectors.length">
              <span :style="{ color: colors.textTertiary, fontSize: '12px', marginRight: '8px' }">GICS 行业:</span>
              <n-tag v-for="s in impactSelectedAlert.affected_sectors" :key="s" size="small" style="margin: 2px">
                {{ s }}
              </n-tag>
            </div>
          </n-space>
        </n-card>
      </n-space>
    </n-drawer-content>
  </n-drawer>

  <!-- 模拟告警对话框 -->
  <n-modal v-model:show="showMockDialog" preset="card" title="模拟告警" style="width: 520px">
    <n-space vertical :size="12">
      <div>
        <span :style="{ fontSize: '13px', color: colors.textSecondary, marginRight: '8px' }">快捷预设:</span>
        <n-space :size="6" inline>
          <n-button v-for="p in mockPresets" :key="p.label" size="small" secondary @click="applyPreset(p)">
            {{ p.label }}
          </n-button>
        </n-space>
      </div>

      <n-form-item label="事件问题" :show-feedback="false">
        <n-input v-model:value="mockForm.question" type="textarea" :rows="2" placeholder="如: Will the US launch military strikes on Iran?" />
      </n-form-item>

      <n-form-item label="事件描述（可选）" :show-feedback="false">
        <n-input v-model:value="mockForm.description" type="textarea" :rows="2" placeholder="事件的详细描述" />
      </n-form-item>

      <n-grid :cols="3" :x-gap="12">
        <n-gi>
          <n-form-item label="分类" :show-feedback="false">
            <n-select
              v-model:value="mockForm.category"
              :options="[
                { label: '政治', value: 'politics' },
                { label: '经济', value: 'economics' },
                { label: '科技', value: 'tech' },
                { label: '其他', value: 'other' },
              ]"
            />
          </n-form-item>
        </n-gi>
        <n-gi>
          <n-form-item label="变动前赔率" :show-feedback="false">
            <n-input-number v-model:value="mockForm.price_before" :min="0" :max="1" :step="0.05" />
          </n-form-item>
        </n-gi>
        <n-gi>
          <n-form-item label="变动后赔率" :show-feedback="false">
            <n-input-number v-model:value="mockForm.price_after" :min="0" :max="1" :step="0.05" />
          </n-form-item>
        </n-gi>
      </n-grid>

      <n-form-item label="告警类型" :show-feedback="false">
        <n-radio-group v-model:value="mockForm.alert_type">
          <n-radio value="spike_5m">5分钟异动</n-radio>
          <n-radio value="spike_1h">1小时异动</n-radio>
          <n-radio value="spike_24h">24小时异动</n-radio>
        </n-radio-group>
      </n-form-item>

      <div style="text-align: right">
        <n-button type="primary" :loading="mockLoading" @click="submitMock">
          触发模拟告警
        </n-button>
      </div>
    </n-space>
  </n-modal>
</template>
