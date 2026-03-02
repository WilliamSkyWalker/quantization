<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getUniverse } from '../api'
import { formatDate } from '../utils/format'
import '../utils/echarts'
import StockTable from '../components/StockTable.vue'
import VChart from 'vue-echarts'

const loading = ref(false)
const date = ref('')
const result = ref<any>(null)

const pieOption = ref<any>(null)

async function loadData() {
  loading.value = true
  try {
    const { data } = await getUniverse(date.value || undefined)
    result.value = data
    if (!date.value) date.value = data.date

    // Industry pie chart
    const dist = data.industry_distribution || {}
    const pieData = Object.entries(dist)
      .sort((a: any, b: any) => b[1] - a[1])
      .slice(0, 15)
      .map(([name, value]) => ({ name, value }))

    pieOption.value = {
      tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
      series: [{
        type: 'pie',
        radius: ['30%', '70%'],
        data: pieData,
        label: { fontSize: 11 },
      }],
    }
  } finally {
    loading.value = false
  }
}

function handleDateUpdate(ts: number | null) {
  date.value = ts ? formatDate(ts) : ''
  loadData()
}

onMounted(loadData)
</script>

<template>
  <div>
    <n-card hoverable style="margin-bottom: 20px">
      <n-space align="center">
        <n-date-picker
          type="date"
          :value="date ? new Date(date).getTime() : null"
          @update:value="handleDateUpdate"
          clearable
          placeholder="选择日期"
        />
        <n-button type="primary" @click="loadData" :loading="loading">查询</n-button>
      </n-space>
      <span v-if="result" style="margin-left: 16px; color: #909399">
        共 {{ result.total }} 只 | 涨停 {{ result.limit_up }} 只 | 跌停 {{ result.limit_down }} 只
      </span>
    </n-card>

    <n-grid :cols="24" :x-gap="20">
      <n-gi :span="16">
        <n-card hoverable title="股票池">
          <StockTable :stocks="result?.stocks || []" :loading="loading" />
        </n-card>
      </n-gi>
      <n-gi :span="8">
        <n-card hoverable title="行业分布">
          <v-chart v-if="pieOption" :option="pieOption" style="height: 400px; width: 100%" autoresize />
          <n-empty v-else description="无数据" />
        </n-card>
      </n-gi>
    </n-grid>
  </div>
</template>
