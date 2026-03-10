<script setup lang="ts">
import { h } from 'vue'
import type { DataTableColumns } from 'naive-ui'
import { pnlColor } from '../theme'

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
      return h('span', { style: { color: pnlColor(row.pnl), fontWeight: 600 } }, row.pnl?.toFixed(2))
    },
  },
  {
    title: '盈亏%', key: 'pnl_pct', width: 80, align: 'right',
    render: (row: any) => {
      return h('span', { style: { color: pnlColor(row.pnl_pct), fontWeight: 600 } }, row.pnl_pct != null ? (row.pnl_pct * 100).toFixed(2) + '%' : '-')
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
