<script setup lang="ts">
import { computed } from 'vue'
import '../utils/echarts'
import VChart from 'vue-echarts'
import { colors } from '../theme'

const props = defineProps<{
  data: { year: number; month: number; return: number }[]
  height?: string
}>()

const option = computed(() => {
  const years = [...new Set(props.data.map(d => d.year))].sort()
  const months = ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月']
  const values = props.data.map(d => [d.month - 1, years.indexOf(d.year), d.return])
  const max = Math.max(...props.data.map(d => Math.abs(d.return)), 0.05)

  return {
    tooltip: {
      formatter: (p: any) => {
        return `${years[p.value[1]]}年${months[p.value[0]]}<br/>收益: ${(p.value[2] * 100).toFixed(2)}%`
      },
    },
    grid: { left: 60, right: 80, top: 10, bottom: 30 },
    xAxis: { type: 'category', data: months, splitArea: { show: true } },
    yAxis: { type: 'category', data: years.map(String), splitArea: { show: true } },
    visualMap: {
      min: -max,
      max: max,
      calculable: true,
      orient: 'vertical',
      right: 0,
      top: 'center',
      inRange: {
        color: [colors.heatmapNeg, colors.heatmapZero, colors.heatmapPos],
      },
      formatter: (v: number) => `${(v * 100).toFixed(1)}%`,
    },
    series: [{
      type: 'heatmap',
      data: values,
      label: {
        show: true,
        formatter: (p: any) => `${(p.value[2] * 100).toFixed(1)}%`,
        fontSize: 10,
      },
    }],
  }
})
</script>

<template>
  <v-chart :option="option" :style="{ height: height || '300px', width: '100%' }" autoresize />
</template>
