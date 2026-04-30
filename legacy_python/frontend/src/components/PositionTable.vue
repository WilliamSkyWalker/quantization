<script setup lang="ts">
import { h } from 'vue'
import type { DataTableColumns } from 'naive-ui'
import { pnlColor } from '../theme'
import { useResponsive } from '../composables/useResponsive'

defineProps<{
  positions: any[]
  loading?: boolean
}>()

const { isMobile } = useResponsive()

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
  <!-- Mobile: card list -->
  <div v-if="isMobile" class="card-list">
    <div v-for="p in positions" :key="p.ts_code" class="pos-card">
      <div class="pos-header">
        <span class="pos-code">{{ p.ts_code }}</span>
        <span class="pos-name">{{ p.name }}</span>
        <span class="pos-pnl" :style="{ color: pnlColor(p.pnl_pct) }">
          {{ p.pnl_pct != null ? (p.pnl_pct * 100).toFixed(2) + '%' : '-' }}
        </span>
      </div>
      <div class="pos-body">
        <div class="pos-item"><span class="pos-label">持仓</span><span>{{ p.volume }}</span></div>
        <div class="pos-item"><span class="pos-label">成本</span><span>{{ p.cost_basis?.toFixed(2) }}</span></div>
        <div class="pos-item"><span class="pos-label">现价</span><span>{{ p.current_price?.toFixed(2) }}</span></div>
        <div class="pos-item">
          <span class="pos-label">盈亏</span>
          <span :style="{ color: pnlColor(p.pnl), fontWeight: 600 }">{{ p.pnl?.toFixed(0) }}</span>
        </div>
      </div>
    </div>
  </div>

  <!-- Desktop: table -->
  <n-data-table
    v-else
    :columns="columns"
    :data="positions"
    :loading="loading"
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
.pos-card {
  border: 1px solid #e8e8e8;
  border-radius: 8px;
  padding: 10px 12px;
  background: #fff;
}
.pos-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.pos-code {
  font-weight: 600;
  font-size: 13px;
}
.pos-name {
  font-size: 13px;
  color: #666;
  flex: 1;
}
.pos-pnl {
  font-weight: 700;
  font-size: 15px;
}
.pos-body {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr 1fr;
  gap: 4px;
  font-size: 12px;
}
.pos-item {
  display: flex;
  flex-direction: column;
  align-items: center;
}
.pos-label {
  color: #999;
  font-size: 11px;
  margin-bottom: 2px;
}
</style>
