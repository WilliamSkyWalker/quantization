<script setup lang="ts">
import { h } from 'vue'
import type { DataTableColumns } from 'naive-ui'
import { pnlColor } from '../theme'

const props = defineProps<{
  stocks: any[]
  loading?: boolean
}>()

const emit = defineEmits<{
  (e: 'row-click', row: any): void
}>()

const columns: DataTableColumns = [
  { title: '代码', key: 'ts_code', width: 100, sorter: 'default' },
  { title: '名称', key: 'name', width: 90 },
  { title: '行业', key: 'industry_name', width: 90 },
  {
    title: '得分', key: 'score', width: 80, sorter: (a: any, b: any) => (a.score ?? 0) - (b.score ?? 0),
    render: (row: any) => row.score != null ? row.score.toFixed(3) : '-',
  },
  {
    title: '权重', key: 'weight', width: 80, sorter: (a: any, b: any) => (a.weight ?? 0) - (b.weight ?? 0),
    render: (row: any) => row.weight != null ? (row.weight * 100).toFixed(2) + '%' : '-',
  },
  {
    title: '收盘价', key: 'close', width: 80, sorter: (a: any, b: any) => (a.close ?? 0) - (b.close ?? 0),
    render: (row: any) => row.close != null ? row.close.toFixed(2) : '-',
  },
  {
    title: '涨跌幅', key: 'pct_chg', width: 80, sorter: (a: any, b: any) => (a.pct_chg ?? 0) - (b.pct_chg ?? 0),
    render: (row: any) => {
      return h('span', { style: { color: pnlColor(row.pct_chg), fontWeight: 600 } }, row.pct_chg != null ? row.pct_chg.toFixed(2) + '%' : '-')
    },
  },
  {
    title: '成交额(万)', key: 'amount', width: 100, sorter: (a: any, b: any) => (a.amount ?? 0) - (b.amount ?? 0),
    render: (row: any) => row.amount != null ? (row.amount / 10000).toFixed(0) : '-',
  },
]

function handleRowClick(row: any) {
  emit('row-click', row)
}

const rowProps = (row: any) => ({
  style: 'cursor: pointer',
  onClick: () => handleRowClick(row),
})
</script>

<template>
  <n-data-table
    :columns="columns"
    :data="stocks"
    :loading="loading"
    :row-props="rowProps"
    striped
    size="small"
    :max-height="600"
    style="width: 100%"
  />
</template>
