<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import { useMessage, NIcon } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { PlayOutline, RocketOutline, FlashOutline } from '@vicons/ionicons5'
import { startBacktest, getDataStatus, getBacktestHistory, getBacktestResult } from '../api'
import { useTaskPolling } from '../composables/useTaskPolling'
import { formatDate } from '../utils/format'
import NavChart from '../components/NavChart.vue'
import DrawdownChart from '../components/DrawdownChart.vue'
import MonthlyHeatmap from '../components/MonthlyHeatmap.vue'
import IndustryBar from '../components/IndustryBar.vue'
import TradeLog from '../components/TradeLog.vue'
import { colors } from '../theme'

const message = useMessage()
const dateRange = ref<[string, string]>(['2020-01-01', '2024-12-31'])
const latestTradeDate = ref('')
const historyList = ref<any[]>([])
const selectedHistoryId = ref<number | null>(null)
const resultSource = ref<'live' | 'saved' | null>(null)
const historyLoading = ref(false)
const { loading, taskId, result, start, stopPolling, taskStore } = useTaskPolling({
  taskLabel: '回测',
})

// When a live backtest completes, mark source and refresh history
watch(result, (val, oldVal) => {
  if (val && !oldVal && resultSource.value === null) {
    resultSource.value = 'live'
    loadHistory()
  }
})

async function loadHistory() {
  try {
    const { data } = await getBacktestHistory()
    historyList.value = data.items || []
  } catch { /* ignore */ }
}

async function loadSavedResult(id: number) {
  selectedHistoryId.value = id
  historyLoading.value = true
  try {
    const { data } = await getBacktestResult(id)
    result.value = data
    resultSource.value = 'saved'
  } catch {
    message.error('加载历史回测失败')
  } finally {
    historyLoading.value = false
  }
}

onMounted(async () => {
  historyLoading.value = true
  try {
    const [, statusRes] = await Promise.allSettled([loadHistory(), getDataStatus()])
    if (statusRes.status === 'fulfilled' && statusRes.value.data.latest_trade_date) {
      latestTradeDate.value = statusRes.value.data.latest_trade_date
    }
  } catch { /* ignore */ }

  // Auto-load latest saved result
  if (!result.value && historyList.value.length > 0) {
    await loadSavedResult(historyList.value[0].id)
  } else {
    historyLoading.value = false
  }
})

function setFullBacktest() {
  if (!latestTradeDate.value) {
    message.warning('无法获取最新交易日，请先下载数据')
    return
  }
  dateRange.value = ['2018-06-01', latestTradeDate.value]
}

function setQuickBacktest() {
  if (!latestTradeDate.value) {
    message.warning('无法获取最新交易日，请先下载数据')
    return
  }
  const d = new Date(latestTradeDate.value)
  d.setFullYear(d.getFullYear() - 2)
  const twoYearsAgo = d.toISOString().slice(0, 10)
  dateRange.value = [twoYearsAgo, latestTradeDate.value]
}

function computeIndustryContributions(attr: any[]) {
  if (!attr?.length) return []
  return attr
    .filter((a: any) => a.industry_name && a.contribution != null)
    .map((a: any) => ({ industry: a.industry_name, contribution: a.contribution }))
}

const holdingsColumns: DataTableColumns = [
  { title: '代码', key: 'ts_code', width: 120 },
  { title: '名称', key: 'name', width: 100 },
  {
    title: '权重', key: 'weight', width: 100,
    render: (row: any) => (row.weight * 100).toFixed(2) + '%',
  },
  {
    title: '得分', key: 'score', width: 100,
    render: (row: any) => row.score?.toFixed(3) || '-',
  },
]

function handleStartDateUpdate(ts: number | null) {
  if (ts) {
    dateRange.value[0] = formatDate(ts)
  }
}

function handleEndDateUpdate(ts: number | null) {
  if (ts) {
    dateRange.value[1] = formatDate(ts)
  }
}

async function runBacktest() {
  if (!dateRange.value?.[0] || !dateRange.value?.[1]) {
    message.warning('请选择日期范围')
    return
  }
  stopPolling()
  resultSource.value = null
  selectedHistoryId.value = null
  try {
    const { data } = await startBacktest(dateRange.value[0], dateRange.value[1])
    taskStore.trackTask(data.task_id, `回测 ${dateRange.value[0]}~${dateRange.value[1]}`)
    message.success('回测任务已启动')
    start(data.task_id)
  } catch (e: any) {
    message.error('启动回测失败')
  }
}

const summaryItems = [
  { key: '总收益', color: colors.primary },
  { key: '年化收益', color: colors.success },
  { key: '最大回撤', color: colors.error },
  { key: '夏普比率', color: colors.warning },
]
</script>

<template>
  <div>
    <!-- Controls -->
    <n-card hoverable style="margin-bottom: 20px">
      <n-space align="center">
        <n-date-picker
          type="date"
          :value="dateRange[0] ? new Date(dateRange[0]).getTime() : null"
          @update:value="handleStartDateUpdate"
          placeholder="开始日期"
        />
        <span>~</span>
        <n-date-picker
          type="date"
          :value="dateRange[1] ? new Date(dateRange[1]).getTime() : null"
          @update:value="handleEndDateUpdate"
          placeholder="结束日期"
        />
        <n-button type="primary" @click="runBacktest" :loading="loading" :disabled="historyLoading">
          <template #icon><n-icon><PlayOutline /></n-icon></template>
          运行回测
        </n-button>
        <n-divider vertical />
        <n-button @click="setFullBacktest" :disabled="!latestTradeDate || loading || historyLoading" secondary>
          <template #icon><n-icon><RocketOutline /></n-icon></template>
          完整回测 (2018~至今)
        </n-button>
        <n-button @click="setQuickBacktest" :disabled="!latestTradeDate || loading || historyLoading" secondary>
          <template #icon><n-icon><FlashOutline /></n-icon></template>
          快速回测 (近2年)
        </n-button>
      </n-space>
    </n-card>

    <!-- History selector -->
    <n-card hoverable style="margin-bottom: 20px" v-if="historyList.length">
      <n-space align="center">
        <span :style="{ fontSize: '14px', color: colors.textSecondary }">历史回测:</span>
        <n-select
          :value="selectedHistoryId"
          @update:value="loadSavedResult"
          :options="historyList.map((h: any) => ({ label: `#${h.id}  ${h.start_date} ~ ${h.end_date}  (${h.summary_headline})  ${h.created_at || ''}`, value: h.id }))"
          placeholder="选择历史回测"
          style="min-width: 420px"
          clearable
        />
        <n-tag v-if="resultSource === 'saved'" type="info" size="small">已存结果</n-tag>
        <n-tag v-else-if="resultSource === 'live'" type="success" size="small">实时计算</n-tag>
      </n-space>
    </n-card>

    <!-- History loading hint -->
    <n-card hoverable style="margin-bottom: 20px" v-if="historyLoading">
      <div style="text-align: center; padding: 40px 0">
        <n-spin size="large" />
        <div :style="{ marginTop: '12px', color: colors.textTertiary }">加载历史回测数据...</div>
      </div>
    </n-card>

    <!-- Loading hint -->
    <n-card hoverable style="margin-bottom: 20px" v-if="loading">
      <div style="text-align: center; padding: 40px 0">
        <n-spin size="large" />
        <div :style="{ marginTop: '12px', color: colors.textTertiary }">
          {{ taskId ? (taskStore.tasks[taskId]?.message || '回测中...') : '启动中...' }}
        </div>
        <n-progress
          v-if="taskId && taskStore.tasks[taskId]"
          type="line"
          :percentage="taskStore.tasks[taskId]?.progress ?? 0"
          :height="12"
          style="max-width: 400px; margin: 16px auto 0"
        />
      </div>
    </n-card>

    <!-- Results -->
    <template v-if="result">
      <!-- Summary cards -->
      <n-grid :cols="4" :x-gap="16" style="margin-bottom: 20px">
        <n-gi v-for="item in summaryItems" :key="item.key">
          <n-card hoverable>
            <div style="display: flex; align-items: center; gap: 12px">
              <div>
                <div style="font-size: 20px; font-weight: 600">{{ result.summary?.[item.key] || '-' }}</div>
                <div :style="{ fontSize: '12px', color: colors.textTertiary }">{{ item.key }}</div>
              </div>
            </div>
          </n-card>
        </n-gi>
      </n-grid>

      <!-- All summary metrics -->
      <n-card hoverable style="margin-bottom: 20px" v-if="result.summary" title="绩效指标">
        <n-descriptions :column="4" bordered label-placement="left" size="small">
          <n-descriptions-item
            v-for="(value, key) in result.summary"
            :key="key"
            :label="String(key)"
          >
            {{ value }}
          </n-descriptions-item>
        </n-descriptions>
      </n-card>

      <!-- NAV Chart -->
      <n-card hoverable style="margin-bottom: 20px" v-if="result.nav?.length" title="净值曲线">
        <NavChart :nav="result.nav" :benchmark="result.benchmark" />
      </n-card>

      <!-- Drawdown -->
      <n-card hoverable style="margin-bottom: 20px" v-if="result.drawdown?.length" title="回撤">
        <DrawdownChart :data="result.drawdown" />
      </n-card>

      <!-- Monthly heatmap -->
      <n-card hoverable style="margin-bottom: 20px" v-if="result.monthly?.length" title="月度收益热力图">
        <MonthlyHeatmap :data="result.monthly" />
      </n-card>

      <!-- Industry Attribution -->
      <n-card hoverable style="margin-bottom: 20px" v-if="result.attribution?.length" title="行业归因">
        <IndustryBar :data="computeIndustryContributions(result.attribution)" />
      </n-card>

      <!-- Latest Holdings -->
      <n-card hoverable style="margin-bottom: 20px" v-if="result.holdings?.length" title="最新持仓">
        <n-data-table :columns="holdingsColumns" :data="result.holdings" striped size="small" />
      </n-card>

      <!-- Trades -->
      <n-card hoverable v-if="result.trades?.length">
        <template #header>交易记录 ({{ result.trades.length }}笔)</template>
        <TradeLog :trades="result.trades" />
      </n-card>
    </template>

    <n-empty v-else-if="!loading && !historyLoading" description="选择日期范围并点击运行回测" />
  </div>
</template>
