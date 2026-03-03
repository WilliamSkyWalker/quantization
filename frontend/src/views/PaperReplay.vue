<script setup lang="ts">
import { ref } from 'vue'
import { useMessage, NIcon } from 'naive-ui'
import { PlayOutline } from '@vicons/ionicons5'
import { startPaperReplay } from '../api'
import { useTaskPolling } from '../composables/useTaskPolling'
import { formatDate } from '../utils/format'
import NavChart from '../components/NavChart.vue'

const message = useMessage()
const dateRange = ref<[string, string]>(['2020-01-01', '2024-12-31'])
const resetAccount = ref(true)
const capital = ref(1000000)
const { loading, taskId, result, start, stopPolling, taskStore } = useTaskPolling({
  taskLabel: '回放',
})

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

async function runReplay() {
  if (!dateRange.value?.[0] || !dateRange.value?.[1]) {
    message.warning('请选择日期范围')
    return
  }
  stopPolling()
  try {
    const { data } = await startPaperReplay(
      dateRange.value[0],
      dateRange.value[1],
      resetAccount.value,
      capital.value,
    )
    taskStore.trackTask(data.task_id, `回放 ${dateRange.value[0]}~${dateRange.value[1]}`)
    message.success('回放任务已启动')
    start(data.task_id)
  } catch {
    message.error('启动回放失败')
  }
}
</script>

<template>
  <div>
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
        <n-input-number v-model:value="capital" :min="10000" :step="100000" style="width: 160px" />
        <n-checkbox v-model:checked="resetAccount">重置账户</n-checkbox>
        <n-button type="primary" @click="runReplay" :loading="loading">
          <template #icon><n-icon><PlayOutline /></n-icon></template>
          开始回放
        </n-button>
      </n-space>
    </n-card>

    <!-- Loading -->
    <n-card hoverable style="margin-bottom: 20px" v-if="loading">
      <div style="text-align: center; padding: 40px 0">
        <n-spin size="large" />
        <div style="margin-top: 12px; color: #909399">
          {{ taskId ? (taskStore.tasks[taskId]?.message || '回放中...') : '启动中...' }}
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

    <template v-if="result">
      <n-card hoverable style="margin-bottom: 20px">
        <n-descriptions :column="3" bordered label-placement="left" size="small">
          <n-descriptions-item label="开始日期">{{ result.start_date }}</n-descriptions-item>
          <n-descriptions-item label="结束日期">{{ result.end_date }}</n-descriptions-item>
          <n-descriptions-item label="信号数">{{ result.signal_count }}</n-descriptions-item>
        </n-descriptions>
      </n-card>

      <n-card hoverable v-if="result.nav?.length" title="回放净值曲线">
        <NavChart :nav="result.nav" />
      </n-card>
    </template>

    <n-empty v-else-if="!loading" description="选择日期范围并开始回放" />
  </div>
</template>
