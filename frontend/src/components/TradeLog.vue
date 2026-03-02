<script setup lang="ts">
import { h } from 'vue'
import { NTag } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'

defineProps<{
  trades: any[]
  loading?: boolean
}>()

const columns: DataTableColumns = [
  {
    title: '日期', key: 'trade_date', width: 100,
    render: (row: any) => row.trade_date || row.date,
  },
  { title: '代码', key: 'ts_code', width: 100 },
  { title: '名称', key: 'name', width: 90 },
  {
    title: '方向', key: 'direction', width: 60,
    render: (row: any) => h(
      NTag,
      { type: row.direction === 'BUY' ? 'error' : 'success', size: 'small' },
      { default: () => row.direction === 'BUY' ? '买入' : '卖出' }
    ),
  },
  {
    title: '价格', key: 'price', width: 80, align: 'right',
    render: (row: any) => row.price?.toFixed(2),
  },
  {
    title: '成交量', key: 'filled_volume', width: 80, align: 'right',
    render: (row: any) => row.filled_volume || row.volume,
  },
  {
    title: '成交额', key: 'amount', width: 100, align: 'right',
    render: (row: any) => row.amount?.toFixed(0),
  },
  {
    title: '原因', key: 'reason', minWidth: 120,
    ellipsis: { tooltip: true },
  },
]
</script>

<template>
  <n-data-table
    :columns="columns"
    :data="trades"
    :loading="loading"
    striped
    size="small"
    :max-height="500"
    style="width: 100%"
  />
</template>
