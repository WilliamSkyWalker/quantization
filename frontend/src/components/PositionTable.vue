<script setup lang="ts">
import { h } from 'vue'
import type { DataTableColumns } from 'naive-ui'

defineProps<{
  positions: any[]
  loading?: boolean
}>()

const columns: DataTableColumns = [
  { title: '代码', key: 'ts_code', width: 100 },
  { title: '名称', key: 'name', width: 90 },
  { title: '持仓量', key: 'volume', width: 80, align: 'right' },
  {
    title: '成本价', key: 'cost_basis', width: 80, align: 'right',
    render: (row: any) => row.cost_basis?.toFixed(2),
  },
  {
    title: '现价', key: 'current_price', width: 80, align: 'right',
    render: (row: any) => row.current_price?.toFixed(2),
  },
  {
    title: '市值', key: 'market_value', width: 100, align: 'right',
    render: (row: any) => row.market_value?.toFixed(0),
  },
  {
    title: '盈亏', key: 'pnl', width: 90, align: 'right',
    render: (row: any) => {
      const color = row.pnl > 0 ? '#f56c6c' : row.pnl < 0 ? '#67c23a' : '#999'
      return h('span', { style: { color } }, row.pnl?.toFixed(2))
    },
  },
  {
    title: '盈亏%', key: 'pnl_pct', width: 80, align: 'right',
    render: (row: any) => {
      const color = row.pnl_pct > 0 ? '#f56c6c' : row.pnl_pct < 0 ? '#67c23a' : '#999'
      return h('span', { style: { color } }, row.pnl_pct != null ? (row.pnl_pct * 100).toFixed(2) + '%' : '-')
    },
  },
]
</script>

<template>
  <n-data-table
    :columns="columns"
    :data="positions"
    :loading="loading"
    striped
    size="small"
    style="width: 100%"
  />
</template>
