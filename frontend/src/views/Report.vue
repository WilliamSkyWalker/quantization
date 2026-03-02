<script setup lang="ts">
import { ref } from 'vue'
import { useMessage, NIcon } from 'naive-ui'
import { DocumentTextOutline } from '@vicons/ionicons5'
import type { DataTableColumns } from 'naive-ui'
import { generateReport, getTaskStatus } from '../api'
import { useTaskPolling } from '../composables/useTaskPolling'
import { formatDate } from '../utils/format'
import NavChart from '../components/NavChart.vue'
import DrawdownChart from '../components/DrawdownChart.vue'
import IndustryBar from '../components/IndustryBar.vue'

const message = useMessage()
const dateRange = ref<[string, string]>(['2020-01-01', '2024-12-31'])
const { loading, taskId, result, start, stopPolling, taskStore } = useTaskPolling({
  fetchResult: getTaskStatus,
  taskLabel: '报告生成',
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

async function generate() {
  if (!dateRange.value?.[0] || !dateRange.value?.[1]) {
    message.warning('请选择日期范围')
    return
  }
  stopPolling()
  try {
    const { data } = await generateReport(dateRange.value[0], dateRange.value[1])
    taskStore.trackTask(data.task_id, `生成报告 ${dateRange.value[0]}~${dateRange.value[1]}`)
    message.success('报告生成任务已启动')
    start(data.task_id)
  } catch {
    message.error('启动失败')
  }
}

function computeDrawdown(nav: { date: string; nav: number }[]) {
  let peak = 0
  return nav.map(d => {
    peak = Math.max(peak, d.nav)
    return { date: d.date, drawdown: (d.nav - peak) / peak }
  })
}

function computeIndustryContributions(attr: any[]) {
  if (!attr?.length) return []
  return attr
    .filter(a => a.industry_name && a.contribution != null)
    .map(a => ({ industry: a.industry_name, contribution: a.contribution }))
}

const holdingsColumns: DataTableColumns = [
  { title: '代码', key: 'ts_code', width: 120 },
  {
    title: '权重', key: 'weight', width: 100,
    render: (row: any) => (row.weight * 100).toFixed(2) + '%',
  },
  {
    title: '得分', key: 'score', width: 100,
    render: (row: any) => row.score?.toFixed(3) || '-',
  },
]
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
        <n-button type="primary" @click="generate" :loading="loading">
          <template #icon><n-icon><DocumentTextOutline /></n-icon></template>
          生成报告
        </n-button>
      </n-space>
    </n-card>

    <!-- Loading -->
    <n-card hoverable style="margin-bottom: 20px" v-if="loading">
      <div style="text-align: center; padding: 40px 0">
        <n-spin size="large" />
        <div style="margin-top: 12px; color: #909399">
          {{ taskId ? (taskStore.tasks[taskId]?.message || '生成中...') : '启动中...' }}
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
        <h2 style="margin: 0 0 16px 0">策略分析报告 ({{ result.period?.start }} ~ {{ result.period?.end }})</h2>
        <n-descriptions :column="4" bordered label-placement="left" size="small" v-if="result.summary">
          <n-descriptions-item
            v-for="(value, key) in result.summary"
            :key="key"
            :label="String(key)"
          >
            {{ value }}
          </n-descriptions-item>
        </n-descriptions>
      </n-card>

      <n-card hoverable style="margin-bottom: 20px" v-if="result.nav?.length" title="净值曲线">
        <NavChart :nav="result.nav" :benchmark="result.benchmark" />
      </n-card>

      <n-card hoverable style="margin-bottom: 20px" v-if="result.nav?.length" title="回撤">
        <DrawdownChart :data="computeDrawdown(result.nav)" />
      </n-card>

      <n-card hoverable style="margin-bottom: 20px" v-if="result.attribution?.length" title="行业归因">
        <IndustryBar :data="computeIndustryContributions(result.attribution)" />
      </n-card>

      <n-card hoverable v-if="result.holdings?.length" title="最新持仓">
        <n-data-table :columns="holdingsColumns" :data="result.holdings" striped size="small" />
      </n-card>
    </template>

    <n-empty v-else-if="!loading" description="选择日期范围生成策略报告" />
  </div>
</template>
