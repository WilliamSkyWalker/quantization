<script setup lang="ts">
import { computed } from 'vue'
import '../utils/echarts'
import VChart from 'vue-echarts'
import { colors } from '../theme'

export interface KlineItem {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

const props = defineProps<{
  data: KlineItem[]
  height?: string
}>()

const option = computed(() => {
  const d = props.data
  if (!d.length) return {}

  const dates = d.map(i => i.date)
  const ohlc = d.map(i => [i.open, i.close, i.low, i.high])
  const volumes = d.map((i, _idx) => ({
    value: i.volume,
    itemStyle: { color: i.close >= i.open ? colors.klineUp : colors.klineDown },
  }))

  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
    },
    grid: [
      { left: 60, right: 20, top: 20, height: '60%' },
      { left: 60, right: 20, top: '76%', height: '16%' },
    ],
    xAxis: [
      {
        type: 'category',
        data: dates,
        boundaryGap: true,
        axisLine: { onZero: false },
        splitLine: { show: false },
        axisLabel: { fontSize: 11 },
      },
      {
        type: 'category',
        gridIndex: 1,
        data: dates,
        boundaryGap: true,
        axisLine: { onZero: false },
        axisLabel: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
      },
    ],
    yAxis: [
      {
        scale: true,
        splitArea: { show: true },
      },
      {
        scale: true,
        gridIndex: 1,
        splitNumber: 2,
        axisLabel: { show: false },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
      },
    ],
    dataZoom: [
      {
        type: 'inside',
        xAxisIndex: [0, 1],
        start: d.length > 120 ? Math.round((1 - 120 / d.length) * 100) : 0,
        end: 100,
      },
      {
        type: 'slider',
        xAxisIndex: [0, 1],
        top: '94%',
        height: 20,
        start: d.length > 120 ? Math.round((1 - 120 / d.length) * 100) : 0,
        end: 100,
      },
    ],
    series: [
      {
        name: 'K线',
        type: 'candlestick',
        data: ohlc,
        itemStyle: {
          color: colors.klineUp,
          color0: colors.klineDown,
          borderColor: colors.klineUp,
          borderColor0: colors.klineDown,
        },
      },
      {
        name: '成交量',
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: volumes,
      },
    ],
  }
})
</script>

<template>
  <v-chart v-if="data.length" :option="option" :style="{ height: height || '500px', width: '100%' }" autoresize />
  <n-empty v-else description="无K线数据" />
</template>
