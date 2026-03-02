<script setup lang="ts">
import { computed } from 'vue'
import '../utils/echarts'
import VChart from 'vue-echarts'

const props = defineProps<{
  data: { industry: string; contribution: number }[]
  height?: string
}>()

const option = computed(() => {
  const sorted = [...props.data].sort((a, b) => b.contribution - a.contribution)
  return {
    tooltip: {
      trigger: 'axis',
      formatter: (params: any) => {
        const p = params[0]
        return `${p.name}<br/>贡献: ${(p.value * 100).toFixed(2)}%`
      },
    },
    grid: { left: 100, right: 20, top: 10, bottom: 30 },
    xAxis: {
      type: 'value',
      axisLabel: { formatter: (v: number) => `${(v * 100).toFixed(1)}%` },
    },
    yAxis: {
      type: 'category',
      data: sorted.map(d => d.industry),
      inverse: true,
    },
    series: [{
      type: 'bar',
      data: sorted.map(d => ({
        value: d.contribution,
        itemStyle: { color: d.contribution >= 0 ? '#67c23a' : '#f56c6c' },
      })),
    }],
  }
})
</script>

<template>
  <v-chart :option="option" :style="{ height: height || '400px', width: '100%' }" autoresize />
</template>
