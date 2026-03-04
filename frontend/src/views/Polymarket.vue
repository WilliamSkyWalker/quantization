<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed, watch } from 'vue'
import { useMessage } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import {
  startMonitor, stopMonitor, getMonitorStatus, getAlerts, markAlertRead, triggerMockAlert,
  runBacktest, getImpactOverview, deleteMockAlerts,
} from '../api/polymarket'
import { useTaskStore } from '../stores/task'

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
      message.success('回测完成')
    } else if (status === 'failed') {
      btRunLoading.value = false
      message.error('回测失败: ' + (btTask.value?.error || ''))
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
    taskStore.trackTask(data.task_id, 'Polymarket 回测')
    message.success('已提交回测任务')
  } catch (e: any) {
    message.error('回测失败: ' + (e.response?.data?.error || e.message))
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
  if (val == null) return '#999'
  if (val > 0.3) return '#18a058'
  if (val > 0.1) return '#5cb85c'
  if (val < -0.3) return '#d03050'
  if (val < -0.1) return '#e88080'
  return '#999'
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

function handleTabChange(tab: string) {
  activeTab.value = tab
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
                <span style="color: #999; font-size: 13px">
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
    <n-tab-pane name="backtest" tab="历史回测">
      <n-space vertical :size="16">
        <!-- 回测配置 + 启动 -->
        <n-card size="small" title="回测配置">
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
              启动回测 (全部已结算市场)
            </n-button>
            <span style="color: #999; font-size: 13px">
              数据准备请前往「数据管理」页
            </span>
          </n-space>
        </n-card>

        <!-- 回测进度 -->
        <n-card v-if="btTaskId && btTask && btTask.status === 'running'" size="small" title="回测进度">
          <n-progress type="line" :percentage="btTask.progress" :indicator-placement="'inside'" />
          <div style="margin-top: 8px; color: #999; font-size: 13px">{{ btTask.message }}</div>
        </n-card>

        <!-- 回测结果 -->
        <template v-if="btResult">
          <!-- 汇总统计 -->
          <n-card size="small" title="回测汇总">
            <n-grid :cols="4" :x-gap="16" :y-gap="12">
              <n-gi>
                <n-statistic label="回测市场数" :value="btResult.summary?.total_markets ?? 0" />
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
              <span style="color: #999; font-size: 13px">告警类型分布:</span>
              <n-tag v-for="(count, type) in (btResult.summary?.alert_type_counts || {})" :key="type as string" size="small">
                {{ alertTypeLabel(type as string) }}: {{ count }}
              </n-tag>
            </n-space>

            <!-- 平均情感 -->
            <n-space style="margin-top: 8px" :size="8" v-if="btResult.summary?.avg_sentiment != null">
              <span style="color: #999; font-size: 13px">平均情感:</span>
              <n-tag :type="btResult.summary.avg_sentiment >= 0 ? 'success' : 'error'" size="small">
                {{ btResult.summary.avg_sentiment.toFixed(3) }}
              </n-tag>
              <span style="color: #999; font-size: 13px">平均置信度:</span>
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
                    <span style="color: #666; font-size: 12px; white-space: nowrap">{{ t.count }} 次</span>
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
                    <span style="color: #666; font-size: 12px; white-space: nowrap">{{ s.count }} 次</span>
                  </div>
                </n-space>
              </n-card>
            </n-gi>
          </n-grid>

          <!-- 市场回测详情 -->
          <n-card size="small" title="市场回测详情" :segmented="{ content: true }">
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

          <!-- 回测告警列表 -->
          <n-card size="small" title="回测触发的告警" :segmented="{ content: true }">
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
                  <span style="color: #999; font-size: 13px">平均情感:</span>
                  <n-tag :type="impactData.summary.avg_sentiment >= 0 ? 'success' : 'error'" size="small">
                    {{ impactData.summary.avg_sentiment.toFixed(3) }}
                  </n-tag>
                </template>
                <template v-if="impactData.summary?.avg_confidence != null">
                  <span style="color: #999; font-size: 13px">平均置信度:</span>
                  <n-tag size="small">{{ impactData.summary.avg_confidence.toFixed(3) }}</n-tag>
                </template>
                <template v-if="impactData.summary?.date_range">
                  <span style="color: #999; font-size: 13px">时间范围:</span>
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
                        color="#409eff"
                      />
                      <span style="color: #666; font-size: 12px; min-width: 40px; text-align: right">{{ count }}</span>
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
                        color="#e6a23c"
                      />
                      <span style="color: #666; font-size: 12px; min-width: 40px; text-align: right">{{ count }}</span>
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
                        <tr style="border-bottom: 1px solid #e0e0e0; color: #999; font-size: 12px">
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
                          :style="{ backgroundColor: sentimentBg(ind.avg_sentiment), borderBottom: '1px solid #f5f5f5' }"
                        >
                          <td style="padding: 6px 8px; font-weight: 500">{{ ind.industry }}</td>
                          <td style="padding: 6px 8px; text-align: right">{{ ind.count }}</td>
                          <td style="padding: 6px 8px; text-align: right; font-weight: 600" :style="{ color: sentimentColor(ind.avg_sentiment) }">
                            {{ ind.avg_sentiment != null ? ind.avg_sentiment.toFixed(3) : '-' }}
                          </td>
                          <td style="padding: 6px 8px; text-align: right; color: #666">
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
                        <tr style="border-bottom: 1px solid #e0e0e0; color: #999; font-size: 12px">
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
                          :style="{ backgroundColor: sentimentBg(s.avg_sentiment), borderBottom: '1px solid #f5f5f5' }"
                        >
                          <td style="padding: 6px 8px; font-weight: 500">{{ s.name }}</td>
                          <td style="padding: 6px 4px; color: #999; font-size: 12px">{{ s.code }}</td>
                          <td style="padding: 6px 8px; text-align: right">{{ s.count }}</td>
                          <td style="padding: 6px 8px; text-align: right; font-weight: 600" :style="{ color: sentimentColor(s.avg_sentiment) }">
                            {{ s.avg_sentiment != null ? s.avg_sentiment.toFixed(3) : '-' }}
                          </td>
                          <td style="padding: 6px 8px; text-align: center">
                            <span v-if="s.bullish" style="color: #18a058; font-size: 12px">{{ s.bullish }}↑</span>
                            <span v-if="s.bullish && s.bearish" style="color: #ccc; margin: 0 2px">/</span>
                            <span v-if="s.bearish" style="color: #d03050; font-size: 12px">{{ s.bearish }}↓</span>
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
                <div style="display: flex; justify-content: space-between; font-size: 11px; color: #999; padding: 0 2px">
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
                  <span style="color: #999; font-size: 13px">
                    请先运行「历史回测」或启动「实时监控」生成告警
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
              style="display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid #f0f0f0"
            >
              <n-tag :type="directionTag(t.direction)" size="small" round style="min-width: 56px; text-align: center">
                {{ t.ticker }}
              </n-tag>
              <n-tag size="small" :type="directionTag(t.direction)" :bordered="false">
                {{ directionLabel(t.direction) }}
              </n-tag>
              <span style="color: #999; font-size: 12px; white-space: nowrap">
                {{ (t.confidence * 100).toFixed(0) }}%
              </span>
              <span v-if="t.reasoning" style="color: #666; font-size: 12px; flex: 1; line-height: 1.4">
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
              style="display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid #f0f0f0"
            >
              <n-tag :type="directionTag(s.direction)" size="small" round style="min-width: 56px; text-align: center">
                {{ s.name }}
              </n-tag>
              <span style="color: #999; font-size: 12px; white-space: nowrap">
                {{ s.code }}
              </span>
              <n-tag size="small" :type="directionTag(s.direction)" :bordered="false">
                {{ directionLabel(s.direction) }}
              </n-tag>
              <span style="color: #999; font-size: 12px; white-space: nowrap">
                {{ (s.confidence * 100).toFixed(0) }}%
              </span>
              <span v-if="s.reasoning" style="color: #666; font-size: 12px; flex: 1; line-height: 1.4">
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
              <span style="color: #999; font-size: 12px; margin-right: 8px">申万行业:</span>
              <n-tag v-for="s in selectedAlert.affected_sw_industries" :key="s" size="small" style="margin: 2px">
                {{ s }}
              </n-tag>
            </div>
            <div v-if="selectedAlert.affected_sectors && selectedAlert.affected_sectors.length">
              <span style="color: #999; font-size: 12px; margin-right: 8px">GICS 行业:</span>
              <n-tag v-for="s in selectedAlert.affected_sectors" :key="s" size="small" style="margin: 2px">
                {{ s }}
              </n-tag>
            </div>
          </n-space>
        </n-card>
      </n-space>
    </n-drawer-content>
  </n-drawer>

  <!-- 回测告警详情抽屉 -->
  <n-drawer v-model:show="btShowAlertDetail" :width="520" placement="right">
    <n-drawer-content v-if="btSelectedAlert" title="回测告警详情">
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
              style="display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid #f0f0f0"
            >
              <n-tag :type="directionTag(t.direction)" size="small" round style="min-width: 56px; text-align: center">
                {{ t.ticker }}
              </n-tag>
              <n-tag size="small" :type="directionTag(t.direction)" :bordered="false">
                {{ directionLabel(t.direction) }}
              </n-tag>
              <span style="color: #999; font-size: 12px; white-space: nowrap">
                {{ (t.confidence * 100).toFixed(0) }}%
              </span>
              <span v-if="t.reasoning" style="color: #666; font-size: 12px; flex: 1; line-height: 1.4">
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
              style="display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid #f0f0f0"
            >
              <n-tag :type="directionTag(s.direction)" size="small" round style="min-width: 56px; text-align: center">
                {{ s.name }}
              </n-tag>
              <span style="color: #999; font-size: 12px; white-space: nowrap">
                {{ s.code }}
              </span>
              <n-tag size="small" :type="directionTag(s.direction)" :bordered="false">
                {{ directionLabel(s.direction) }}
              </n-tag>
              <span style="color: #999; font-size: 12px; white-space: nowrap">
                {{ (s.confidence * 100).toFixed(0) }}%
              </span>
              <span v-if="s.reasoning" style="color: #666; font-size: 12px; flex: 1; line-height: 1.4">
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
              <span style="color: #999; font-size: 12px; margin-right: 8px">申万行业:</span>
              <n-tag v-for="s in btSelectedAlert.affected_sw_industries" :key="s" size="small" style="margin: 2px">
                {{ s }}
              </n-tag>
            </div>
            <div v-if="btSelectedAlert.affected_sectors && btSelectedAlert.affected_sectors.length">
              <span style="color: #999; font-size: 12px; margin-right: 8px">GICS 行业:</span>
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
              style="display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid #f0f0f0"
            >
              <n-tag :type="directionTag(t.direction)" size="small" round style="min-width: 56px; text-align: center">
                {{ t.ticker }}
              </n-tag>
              <n-tag size="small" :type="directionTag(t.direction)" :bordered="false">
                {{ directionLabel(t.direction) }}
              </n-tag>
              <span style="color: #999; font-size: 12px; white-space: nowrap">
                {{ (t.confidence * 100).toFixed(0) }}%
              </span>
              <span v-if="t.reasoning" style="color: #666; font-size: 12px; flex: 1; line-height: 1.4">
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
              style="display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid #f0f0f0"
            >
              <n-tag :type="directionTag(s.direction)" size="small" round style="min-width: 56px; text-align: center">
                {{ s.name }}
              </n-tag>
              <span style="color: #999; font-size: 12px; white-space: nowrap">
                {{ s.code }}
              </span>
              <n-tag size="small" :type="directionTag(s.direction)" :bordered="false">
                {{ directionLabel(s.direction) }}
              </n-tag>
              <span style="color: #999; font-size: 12px; white-space: nowrap">
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
              <span style="color: #999; font-size: 12px; margin-right: 8px">申万行业:</span>
              <n-tag v-for="s in impactSelectedAlert.affected_sw_industries" :key="s" size="small" style="margin: 2px">
                {{ s }}
              </n-tag>
            </div>
            <div v-if="impactSelectedAlert.affected_sectors && impactSelectedAlert.affected_sectors.length">
              <span style="color: #999; font-size: 12px; margin-right: 8px">GICS 行业:</span>
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
        <span style="font-size: 13px; color: #666; margin-right: 8px">快捷预设:</span>
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
