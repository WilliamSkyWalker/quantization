<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { useMessage } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import {
  startMonitor, stopMonitor, getMonitorStatus, getAlerts, markAlertRead, triggerMockAlert,
  runBacktest,
} from '../api/polymarket'
import { getTaskStatus } from '../api'
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
const btSelectedAlert = ref<any>(null)
const btShowAlertDetail = ref(false)

// Backtest config
const btConfig = ref({
  use_llm: true,
  spike_5m: 0.05,
  spike_1h: 0.15,
  spike_24h: 0.25,
})

async function doRunBacktest() {
  btRunLoading.value = true
  btResult.value = null
  try {
    const payload: any = {
      use_llm: btConfig.value.use_llm,
      spike_5m: btConfig.value.spike_5m,
      spike_1h: btConfig.value.spike_1h,
      spike_24h: btConfig.value.spike_24h,
    }
    const { data } = await runBacktest(payload)
    taskStore.trackTask(data.task_id, 'Polymarket 回测')
    message.success('已提交回测任务，请在任务完成后查看结果')
    // 轮询结果
    pollBacktestResult(data.task_id)
  } catch (e: any) {
    message.error('回测失败: ' + (e.response?.data?.error || e.message))
  } finally {
    btRunLoading.value = false
  }
}

function pollBacktestResult(taskId: string) {
  const poll = setInterval(async () => {
    try {
      const { data } = await getTaskStatus(taskId)
      if (data.status === 'completed') {
        clearInterval(poll)
        btResult.value = data.result
        message.success('回测完成')
      } else if (data.status === 'failed') {
        clearInterval(poll)
        message.error('回测失败: ' + (data.error || ''))
      }
    } catch {
      clearInterval(poll)
    }
  }, 3000)
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
