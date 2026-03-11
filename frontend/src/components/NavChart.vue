<script setup lang="ts">
import { computed } from 'vue'
import '../utils/echarts'
import VChart from 'vue-echarts'
import { colors } from '../theme'

const props = defineProps<{
  nav: { date: string; nav: number }[]
  benchmark?: { date: string; nav: number }[]
  height?: string
}>()

// Sort ascending (oldest → newest) for left-to-right display
const sortedNav = computed(() => [...props.nav].sort((a, b) => a.date.localeCompare(b.date)))
const sortedBenchmark = computed(() =>
  props.benchmark ? [...props.benchmark].sort((a, b) => a.date.localeCompare(b.date)) : [],
)

const option = computed(() => ({
  tooltip: {
    trigger: 'axis',
    formatter: (params: any) => {
      return `${params[0]?.value[0]}<br/>${params.map((p: any) => `${p.marker} ${p.seriesName}: ${p.value[1]?.toFixed(4) || '-'}`).join('<br/>')}`
    },
  },
  legend: { show: !!(sortedBenchmark.value.length) },
  grid: { left: 60, right: 20, top: 30, bottom: sortedNav.value.length > 100 ? 60 : 30 },
  xAxis: { type: 'category' },
  yAxis: { type: 'value', scale: true },
  dataZoom: sortedNav.value.length > 100 ? [{ type: 'inside' }, { type: 'slider' }] : [],
  series: [
    {
      name: '策略净值',
      type: 'line',
      showSymbol: false,
      lineStyle: { width: 2 },
      itemStyle: { color: colors.chartLine },
      data: sortedNav.value.map(d => [d.date, d.nav]),
    },
    ...(sortedBenchmark.value.length ? [{
      name: '基准净值',
      type: 'line',
      showSymbol: false,
      lineStyle: { width: 1.5, type: 'dashed' as const },
      itemStyle: { color: colors.chartBenchmark },
      data: sortedBenchmark.value.map(d => [d.date, d.nav]),
    }] : []),
  ],
}))
</script>

<template>
  <v-chart :option="option" :style="{ height: height || '350px', width: '100%' }" autoresize />
</template>
