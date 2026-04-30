<script setup lang="ts">
import { h } from 'vue'
import type { DataTableColumns } from 'naive-ui'
import { useMessage } from 'naive-ui'
import { pnlColor } from '../theme'
import { useResponsive } from '../composables/useResponsive'

const props = defineProps<{
  stocks: any[]
  loading?: boolean
  showCopy?: boolean
}>()

const emit = defineEmits<{
  (e: 'row-click', row: any): void
}>()

const message = useMessage()
const { isMobile } = useResponsive()

function copyStockList() {
  const lines = props.stocks.map(s => {
    const weight = s.weight != null ? (s.weight * 100).toFixed(2) + '%' : ''
    return `${s.ts_code}\t${s.name || ''}\t${weight}`
  })
  navigator.clipboard.writeText(lines.join('\n')).then(() => {
    message.success('已复制到剪贴板')
  }).catch(() => {
    message.error('复制失败')
  })
}

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

defineExpose({ copyStockList })
</script>

<template>
  <!-- Mobile: card list -->
  <div v-if="isMobile" class="card-list">
    <div v-for="s in stocks" :key="s.ts_code" class="stock-card" @click="handleRowClick(s)">
      <div class="stock-header">
        <div>
          <span class="stock-code">{{ s.ts_code }}</span>
          <span class="stock-name">{{ s.name }}</span>
        </div>
        <span class="stock-chg" :style="{ color: pnlColor(s.pct_chg) }">
          {{ s.pct_chg != null ? s.pct_chg.toFixed(2) + '%' : '-' }}
        </span>
      </div>
      <div class="stock-body">
        <div class="stock-item"><span class="stock-label">得分</span><span>{{ s.score?.toFixed(3) || '-' }}</span></div>
        <div class="stock-item"><span class="stock-label">权重</span><span>{{ s.weight != null ? (s.weight * 100).toFixed(1) + '%' : '-' }}</span></div>
        <div class="stock-item"><span class="stock-label">行业</span><span>{{ s.industry_name || '-' }}</span></div>
        <div class="stock-item"><span class="stock-label">收盘</span><span>{{ s.close?.toFixed(2) || '-' }}</span></div>
      </div>
    </div>
  </div>

  <!-- Desktop: table -->
  <n-data-table
    v-else
    :columns="columns"
    :data="stocks"
    :loading="loading"
    :row-props="rowProps"
    striped
    size="small"
    style="width: 100%"
  />
</template>

<style scoped>
.card-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.stock-card {
  border: 1px solid #e8e8e8;
  border-radius: 8px;
  padding: 10px 12px;
  background: #fff;
  cursor: pointer;
  transition: background 0.15s;
}
.stock-card:active {
  background: #f5f5f5;
}
.stock-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.stock-code {
  font-weight: 600;
  font-size: 13px;
  margin-right: 6px;
}
.stock-name {
  font-size: 13px;
  color: #666;
}
.stock-chg {
  font-weight: 700;
  font-size: 15px;
}
.stock-body {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr 1fr;
  gap: 4px;
  font-size: 12px;
}
.stock-item {
  display: flex;
  flex-direction: column;
  align-items: center;
}
.stock-label {
  color: #999;
  font-size: 11px;
  margin-bottom: 2px;
}
</style>
