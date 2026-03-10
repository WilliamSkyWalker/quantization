<script setup lang="ts">
import { computed } from 'vue'
import '../utils/echarts'
import VChart from 'vue-echarts'
import { colors } from '../theme'

const props = defineProps<{
  data: { date: string; drawdown: number }[]
  height?: string
}>()

const option = computed(() => ({
  tooltip: {
    trigger: 'axis',
    formatter: (params: any) => {
      const p = params[0]
      return `${p.value[0]}<br/>回撤: ${(p.value[1] * 100).toFixed(2)}%`
    },
  },
  grid: { left: 60, right: 20, top: 10, bottom: props.data.length > 100 ? 60 : 30 },
  xAxis: { type: 'category' },
  yAxis: {
    type: 'value',
    axisLabel: { formatter: (v: number) => `${(v * 100).toFixed(0)}%` },
  },
  dataZoom: props.data.length > 100 ? [{ type: 'inside' }, { type: 'slider' }] : [],
  series: [{
    type: 'line',
    showSymbol: false,
    lineStyle: { width: 1.5, color: colors.chartDrawdown },
    areaStyle: { color: colors.chartDrawdownArea },
    data: props.data.map(d => [d.date, d.drawdown]),
  }],
}))
</script>

<template>
  <v-chart :option="option" :style="{ height: height || '250px', width: '100%' }" autoresize />
</template>
