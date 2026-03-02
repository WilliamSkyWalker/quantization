<script setup lang="ts">
import { computed } from 'vue'
import '../utils/echarts'
import VChart from 'vue-echarts'

const props = defineProps<{
  nav: { date: string; nav: number }[]
  benchmark?: { date: string; nav: number }[]
  height?: string
}>()

const option = computed(() => ({
  tooltip: {
    trigger: 'axis',
    formatter: (params: any) => {
      return `${params[0]?.value[0]}<br/>${params.map((p: any) => `${p.marker} ${p.seriesName}: ${p.value[1]?.toFixed(4) || '-'}`).join('<br/>')}`
    },
  },
  legend: { show: !!(props.benchmark?.length) },
  grid: { left: 60, right: 20, top: 30, bottom: props.nav.length > 100 ? 60 : 30 },
  xAxis: { type: 'category' },
  yAxis: { type: 'value', scale: true },
  dataZoom: props.nav.length > 100 ? [{ type: 'inside' }, { type: 'slider' }] : [],
  series: [
    {
      name: '策略净值',
      type: 'line',
      showSymbol: false,
      lineStyle: { width: 2 },
      itemStyle: { color: '#409eff' },
      data: props.nav.map(d => [d.date, d.nav]),
    },
    ...(props.benchmark?.length ? [{
      name: '基准净值',
      type: 'line',
      showSymbol: false,
      lineStyle: { width: 1.5, type: 'dashed' as const },
      itemStyle: { color: '#999' },
      data: props.benchmark.map(d => [d.date, d.nav]),
    }] : []),
  ],
}))
</script>

<template>
  <v-chart :option="option" :style="{ height: height || '350px', width: '100%' }" autoresize />
</template>
