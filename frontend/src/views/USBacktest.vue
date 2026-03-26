<script setup lang="ts">
import { ref, computed } from 'vue'
import { useMessage, NIcon } from 'naive-ui'
import { PlayOutline, RocketOutline, FlashOutline } from '@vicons/ionicons5'
import { startUSBacktest } from '../api'
import { useTaskPolling } from '../composables/useTaskPolling'
import { useResponsive } from '../composables/useResponsive'
import { formatDate } from '../utils/format'
import NavChart from '../components/NavChart.vue'
import TradeLog from '../components/TradeLog.vue'
import { colors } from '../theme'

const message = useMessage()
const { isMobile } = useResponsive()

const today = new Date().toISOString().slice(0, 10)
const dateRange = ref<[string, string]>(['2020-01-01', today])
const initialCapital = ref(100000)

const { loading, taskId, result, start, stopPolling, taskStore } = useTaskPolling({
  taskLabel: 'US Backtest',
})

function setFullBacktest() {
  dateRange.value = ['2020-01-01', today]
}

function setQuickBacktest() {
  const d = new Date()
  d.setFullYear(d.getFullYear() - 2)
  const twoYearsAgo = d.toISOString().slice(0, 10)
  dateRange.value = [twoYearsAgo, today]
}

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
    message.warning('Please select a date range')
    return
  }
  stopPolling()
  try {
    const { data } = await startUSBacktest(dateRange.value[0], dateRange.value[1], initialCapital.value)
    taskStore.trackTask(data.task_id, `US Backtest ${dateRange.value[0]}~${dateRange.value[1]}`)
    message.success('US Backtest task started')
    start(data.task_id)
  } catch (e: any) {
    message.error('Failed to start US backtest')
  }
}

const summaryItems = computed(() => {
  const stats = result.value?.stats
  if (!stats) return []
  return [
    { label: 'Total Return', value: stats.total_return, color: colors.primary },
    { label: 'Annual Return', value: stats.annual_return, color: colors.success },
    { label: 'Max Drawdown', value: stats.max_drawdown, color: colors.error },
    { label: 'Sharpe Ratio', value: stats.sharpe_ratio, color: colors.warning },
  ]
})

const statsEntries = computed(() => {
  const stats = result.value?.stats
  if (!stats) return []
  const labelMap: Record<string, string> = {
    total_return: 'Total Return',
    annual_return: 'Annual Return',
    max_drawdown: 'Max Drawdown',
    sharpe_ratio: 'Sharpe Ratio',
    calmar_ratio: 'Calmar Ratio',
    win_rate_daily: 'Daily Win Rate',
    total_trades: 'Total Trades',
    benchmark_return: 'S&P 500 Return',
    alpha: 'Alpha',
  }
  return Object.entries(stats).map(([key, value]) => ({
    label: labelMap[key] || key,
    value,
  }))
})
</script>

<template>
  <div>
    <!-- Controls -->
    <n-card hoverable style="margin-bottom: 20px">
      <n-space align="center" wrap :vertical="isMobile">
        <n-date-picker
          type="date"
          :value="dateRange[0] ? new Date(dateRange[0]).getTime() : null"
          @update:value="handleStartDateUpdate"
          placeholder="Start Date"
        />
        <span>~</span>
        <n-date-picker
          type="date"
          :value="dateRange[1] ? new Date(dateRange[1]).getTime() : null"
          @update:value="handleEndDateUpdate"
          placeholder="End Date"
        />
        <n-input-number
          v-model:value="initialCapital"
          :min="1000"
          :step="10000"
          :style="{ width: '180px' }"
          placeholder="Initial Capital"
        >
          <template #prefix>$</template>
        </n-input-number>
        <n-button type="primary" @click="runBacktest" :loading="loading">
          <template #icon><n-icon><PlayOutline /></n-icon></template>
          Run Backtest
        </n-button>
        <n-divider vertical />
        <n-button @click="setFullBacktest" :disabled="loading" secondary>
          <template #icon><n-icon><RocketOutline /></n-icon></template>
          Full Backtest (2020~today)
        </n-button>
        <n-button @click="setQuickBacktest" :disabled="loading" secondary>
          <template #icon><n-icon><FlashOutline /></n-icon></template>
          Quick (2-year)
        </n-button>
      </n-space>
    </n-card>

    <!-- Loading -->
    <n-card hoverable style="margin-bottom: 20px" v-if="loading">
      <div style="text-align: center; padding: 40px 0">
        <n-spin size="large" />
        <div :style="{ marginTop: '12px', color: colors.textTertiary }">
          {{ taskId ? (taskStore.tasks[taskId]?.message || 'Running US backtest...') : 'Starting...' }}
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
      <n-grid :cols="isMobile ? 2 : 4" :x-gap="16" style="margin-bottom: 20px">
        <n-gi v-for="item in summaryItems" :key="item.label">
          <n-card hoverable>
            <div style="display: flex; align-items: center; gap: 12px">
              <div>
                <div style="font-size: 20px; font-weight: 600">{{ item.value || '-' }}</div>
                <div :style="{ fontSize: '12px', color: colors.textTertiary }">{{ item.label }}</div>
              </div>
            </div>
          </n-card>
        </n-gi>
      </n-grid>

      <!-- All metrics -->
      <n-card hoverable style="margin-bottom: 20px" v-if="result.stats" title="Performance Metrics">
        <n-descriptions :column="isMobile ? 1 : 4" bordered label-placement="left" size="small">
          <n-descriptions-item
            v-for="entry in statsEntries"
            :key="entry.label"
            :label="entry.label"
          >
            {{ entry.value }}
          </n-descriptions-item>
        </n-descriptions>
      </n-card>

      <!-- NAV Chart -->
      <n-card hoverable style="margin-bottom: 20px" v-if="result.nav?.length" title="NAV Curve (Strategy vs S&P 500)">
        <NavChart :nav="result.nav" :benchmark="result.benchmark" />
      </n-card>

      <!-- Trade Log -->
      <n-card hoverable v-if="result.trades?.length">
        <template #header>Trade Log ({{ result.trades.length }} trades)</template>
        <TradeLog :trades="result.trades" />
      </n-card>
    </template>

    <n-empty v-else-if="!loading" description="Select date range and click Run Backtest" />
  </div>
</template>
