<script setup lang="ts">
import { ref, h, watch, onUnmounted } from 'vue'
import { useMessage } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { startUSSelect } from '../api'
import { useTaskStore } from '../stores/task'
import { useResponsive } from '../composables/useResponsive'
import { colors, semanticColor } from '../theme'
import { formatDate, todayStr } from '../utils/format'

const { isMobile } = useResponsive()

const message = useMessage()
const taskStore = useTaskStore()
const loading = ref(false)
const progress = ref(0)
const progressMsg = ref('')
const date = ref(todayStr())
const result = ref<any>(null)
let taskWatcher: (() => void) | null = null

function stopWatchingTask() {
  if (taskWatcher) {
    taskWatcher()
    taskWatcher = null
  }
}

function startWatchingTask(id: string) {
  stopWatchingTask()
  taskWatcher = watch(
    () => taskStore.tasks[id],
    (task) => {
      if (!task) return
      progress.value = task.progress ?? 0
      progressMsg.value = task.message || 'Running...'
      if (task.status === 'completed') {
        result.value = task.result
        loading.value = false
        stopWatchingTask()
      } else if (task.status === 'failed' || task.status === 'cancelled') {
        message.error(`Selection failed: ${task.error || 'Cancelled'}`)
        loading.value = false
        stopWatchingTask()
      }
    },
    { immediate: true },
  )
}

async function runSelect() {
  loading.value = true
  result.value = null
  progress.value = 0
  progressMsg.value = 'Starting...'
  stopWatchingTask()
  try {
    const { data } = await startUSSelect(date.value || undefined)
    if (!date.value) {
      date.value = data.date
    }
    taskStore.trackTask(data.task_id, `US Stock Selection ${data.date || ''}`)
    startWatchingTask(data.task_id)
  } catch {
    message.error('Failed to start US stock selection')
    loading.value = false
  }
}

function handleDateUpdate(ts: number | null) {
  date.value = ts ? formatDate(ts) : ''
  result.value = null
}

onUnmounted(stopWatchingTask)

// Desktop table columns
const columns: DataTableColumns = [
  { title: 'Ticker', key: 'ticker', width: 100, sorter: 'default' },
  {
    title: 'Name', key: 'name', width: 120, ellipsis: { tooltip: true },
  },
  {
    title: 'Sector', key: 'sector', width: 160, ellipsis: { tooltip: true },
  },
  {
    title: 'Score', key: 'score', width: 90,
    sorter: (a: any, b: any) => (a.score ?? 0) - (b.score ?? 0),
    render: (row: any) => row.score != null ? row.score.toFixed(3) : '-',
  },
  {
    title: 'Weight', key: 'weight', width: 90,
    sorter: (a: any, b: any) => (a.weight ?? 0) - (b.weight ?? 0),
    render: (row: any) => row.weight != null ? (row.weight * 100).toFixed(2) + '%' : '-',
  },
  {
    title: 'Close', key: 'close', width: 90,
    sorter: (a: any, b: any) => (a.close ?? 0) - (b.close ?? 0),
    render: (row: any) => row.close != null ? row.close.toFixed(2) : '-',
  },
  {
    title: 'Change %', key: 'pct_chg', width: 90,
    sorter: (a: any, b: any) => (a.pct_chg ?? 0) - (b.pct_chg ?? 0),
    render: (row: any) => {
      return h(
        'span',
        { style: { color: semanticColor(row.pct_chg), fontWeight: 600 } },
        row.pct_chg != null ? (row.pct_chg > 0 ? '+' : '') + row.pct_chg.toFixed(2) + '%' : '-',
      )
    },
  },
]
</script>

<template>
  <div>
    <h2 :style="{ margin: '0 0 16px 0', fontSize: '20px', fontWeight: 600, color: colors.textPrimary }">
      US Stock Selection
    </h2>

    <!-- Control bar -->
    <n-card hoverable style="margin-bottom: 20px">
      <n-space align="center" wrap>
        <n-date-picker
          type="date"
          :value="date ? new Date(date).getTime() : null"
          @update:value="handleDateUpdate"
          clearable
          placeholder="Select date"
        />
        <n-button
          :type="result ? 'warning' : 'primary'"
          :loading="loading"
          :disabled="!date"
          @click="runSelect"
        >
          {{ result ? 'Re-run Selection' : 'Run Selection' }}
        </n-button>
      </n-space>
    </n-card>

    <!-- Loading progress -->
    <n-card hoverable style="margin-bottom: 20px" v-if="loading">
      <div style="text-align: center; padding: 40px 0">
        <n-spin size="large" />
        <div :style="{ marginTop: '12px', color: colors.textTertiary }">{{ progressMsg }}</div>
        <n-progress
          type="line"
          :percentage="progress"
          :height="12"
          style="max-width: 400px; margin: 16px auto 0"
        />
      </div>
    </n-card>

    <!-- Summary stats + Results -->
    <template v-if="result">
      <n-card hoverable style="margin-bottom: 20px">
        <n-grid :cols="isMobile ? 2 : 4" :x-gap="16" :y-gap="12">
          <n-gi>
            <n-statistic label="Date" :value="result.date" />
          </n-gi>
          <n-gi>
            <n-statistic label="Selected" :value="result.count ?? result.data?.length ?? 0" />
          </n-gi>
        </n-grid>
      </n-card>

      <!-- Desktop: data table -->
      <n-card hoverable v-if="!isMobile">
        <template #header>
          <span>Top {{ result.data?.length || 0 }} Results</span>
        </template>
        <n-data-table
          :columns="columns"
          :data="result.data || []"
          :bordered="false"
          striped
          size="small"
          :pagination="{ pageSize: 50 }"
          :row-props="() => ({ style: 'cursor: pointer' })"
        />
      </n-card>

      <!-- Mobile: card list -->
      <div v-else>
        <n-card
          v-for="(stock, idx) in (result.data || [])"
          :key="stock.ticker || idx"
          hoverable
          size="small"
          :style="{ marginBottom: '10px' }"
        >
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px">
            <div>
              <span :style="{ fontWeight: 700, fontSize: '15px', color: colors.textPrimary }">
                {{ stock.ticker }}
              </span>
              <span :style="{ marginLeft: '8px', color: colors.textSecondary, fontSize: '13px' }">
                {{ stock.name }}
              </span>
            </div>
            <span
              :style="{
                fontWeight: 700,
                fontSize: '15px',
                color: semanticColor(stock.pct_chg),
              }"
            >
              {{ stock.pct_chg != null ? (stock.pct_chg > 0 ? '+' : '') + stock.pct_chg.toFixed(2) + '%' : '-' }}
            </span>
          </div>
          <n-grid :cols="4" :x-gap="8">
            <n-gi>
              <div :style="{ fontSize: '11px', color: colors.textTertiary }">Score</div>
              <div :style="{ fontWeight: 600, fontSize: '13px' }">
                {{ stock.score != null ? stock.score.toFixed(3) : '-' }}
              </div>
            </n-gi>
            <n-gi>
              <div :style="{ fontSize: '11px', color: colors.textTertiary }">Weight</div>
              <div :style="{ fontWeight: 600, fontSize: '13px' }">
                {{ stock.weight != null ? (stock.weight * 100).toFixed(2) + '%' : '-' }}
              </div>
            </n-gi>
            <n-gi>
              <div :style="{ fontSize: '11px', color: colors.textTertiary }">Sector</div>
              <div :style="{ fontSize: '12px', color: colors.textSecondary, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }">
                {{ stock.sector || '-' }}
              </div>
            </n-gi>
            <n-gi>
              <div :style="{ fontSize: '11px', color: colors.textTertiary }">Close</div>
              <div :style="{ fontWeight: 600, fontSize: '13px' }">
                {{ stock.close != null ? stock.close.toFixed(2) : '-' }}
              </div>
            </n-gi>
          </n-grid>
        </n-card>
      </div>
    </template>

    <n-empty v-if="!loading && !result" description="Select a date and click Run Selection to start" />
  </div>
</template>
