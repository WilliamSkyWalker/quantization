<script setup lang="ts">
import { ref, h, watch, onMounted, onUnmounted } from 'vue'
import { useMessage, useDialog, NIcon } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { PlayOutline, TrashOutline, RefreshOutline } from '@vicons/ionicons5'
import { getUSPaperAccount, getUSPaperPositions, getUSPaperNav, startUSPaperTrade, resetUSPaper } from '../api'
import { useTaskStore } from '../stores/task'
import NavChart from '../components/NavChart.vue'
import { colors, semanticColor } from '../theme'
import { useResponsive } from '../composables/useResponsive'

const { isMobile } = useResponsive()

const message = useMessage()
const dialogCtrl = useDialog()
const taskStore = useTaskStore()
const loading = ref(false)
const tradeLoading = ref(false)
const account = ref<any>(null)
const positions = ref<any[]>([])
const navData = ref<any[]>([])

function fmtUSD(val: number | null | undefined, decimals = 2): string {
  if (val == null) return '-'
  return '$' + val.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
}

async function loadAll() {
  loading.value = true
  try {
    const [acctRes, posRes, navRes] = await Promise.allSettled([
      getUSPaperAccount(),
      getUSPaperPositions(),
      getUSPaperNav(),
    ])
    if (acctRes.status === 'fulfilled') account.value = acctRes.value.data
    if (posRes.status === 'fulfilled') {
      positions.value = posRes.value.data?.data || posRes.value.data || []
    }
    if (navRes.status === 'fulfilled') {
      navData.value = navRes.value.data?.data || navRes.value.data || []
    }
  } finally {
    loading.value = false
  }
}

// --- Trade execution with task tracking ---

let tradeTaskWatcher: (() => void) | null = null

function stopTradeWatcher() {
  if (tradeTaskWatcher) {
    tradeTaskWatcher()
    tradeTaskWatcher = null
  }
}

async function executeTrade() {
  tradeLoading.value = true
  try {
    const { data } = await startUSPaperTrade()
    taskStore.trackTask(data.task_id, 'US Paper Trade')
    message.success('US paper trade task started')

    stopTradeWatcher()
    tradeTaskWatcher = watch(
      () => taskStore.tasks[data.task_id],
      (task) => {
        if (!task) return
        if (task.status === 'completed') {
          message.success('US paper trade completed')
          tradeLoading.value = false
          stopTradeWatcher()
          loadAll()
        } else if (task.status === 'failed' || task.status === 'cancelled') {
          message.error(`Trade failed: ${task.error || 'Cancelled'}`)
          tradeLoading.value = false
          stopTradeWatcher()
        }
      },
      { immediate: true },
    )
  } catch (e: any) {
    message.error('Failed to start trade')
    tradeLoading.value = false
  }
}

// --- Reset ---

function handleReset() {
  dialogCtrl.warning({
    title: 'Reset Account',
    content: 'This will delete all positions, transactions, and NAV history. Continue?',
    positiveText: 'Reset',
    negativeText: 'Cancel',
    onPositiveClick: async () => {
      await resetUSPaper()
      message.success('Account reset')
      loadAll()
    },
  })
}

// --- Position table columns ---

const positionColumns: DataTableColumns = [
  { title: 'Ticker', key: 'ticker', width: 100 },
  { title: 'Volume', key: 'volume', width: 80, align: 'right' },
  {
    title: 'Cost',
    key: 'cost_basis',
    width: 100,
    align: 'right',
    render: (row: any) => h('span', {}, fmtUSD(row.cost_basis)),
  },
  {
    title: 'Price',
    key: 'current_price',
    width: 100,
    align: 'right',
    render: (row: any) => h('span', {}, fmtUSD(row.current_price)),
  },
  {
    title: 'Market Value',
    key: 'market_value',
    width: 120,
    align: 'right',
    render: (row: any) => h('span', {}, fmtUSD(row.market_value, 0)),
  },
  {
    title: 'P&L',
    key: 'pnl',
    width: 100,
    align: 'right',
    render: (row: any) =>
      h('span', { style: { color: semanticColor(row.pnl) } }, fmtUSD(row.pnl)),
  },
  {
    title: 'P&L %',
    key: 'pnl_pct',
    width: 90,
    align: 'right',
    render: (row: any) =>
      h(
        'span',
        { style: { color: semanticColor(row.pnl_pct) } },
        row.pnl_pct != null ? (row.pnl_pct * 100).toFixed(2) + '%' : '-',
      ),
  },
]

// Transform NAV data for NavChart component
function navChartData() {
  return navData.value.map((d: any) => ({
    date: d.nav_date,
    nav: d.nav ?? (d.total_assets ? d.total_assets / (account.value?.initial_capital || 1) : 1),
  }))
}

onMounted(loadAll)
onUnmounted(stopTradeWatcher)
</script>

<template>
  <n-spin :show="loading">
    <!-- Account overview -->
    <n-grid :cols="isMobile ? 2 : 24" :x-gap="isMobile ? 8 : 16" style="margin-bottom: 20px" v-if="account">
      <n-gi :span="isMobile ? 1 : 5">
        <n-card hoverable>
          <div class="metric">
            <div class="metric-label">Initial Capital</div>
            <div class="metric-value">{{ fmtUSD(account.initial_capital, 0) }}</div>
          </div>
        </n-card>
      </n-gi>
      <n-gi :span="isMobile ? 1 : 5">
        <n-card hoverable>
          <div class="metric">
            <div class="metric-label">Cash</div>
            <div class="metric-value">{{ fmtUSD(account.cash, 0) }}</div>
          </div>
        </n-card>
      </n-gi>
      <n-gi :span="isMobile ? 1 : 5">
        <n-card hoverable>
          <div class="metric">
            <div class="metric-label">Total Assets</div>
            <div class="metric-value">{{ fmtUSD(account.total_assets, 0) }}</div>
          </div>
        </n-card>
      </n-gi>
      <n-gi :span="isMobile ? 1 : 5">
        <n-card hoverable>
          <div class="metric">
            <div class="metric-label">P&L</div>
            <div class="metric-value" :style="{ color: semanticColor(account.pnl) }">
              {{ fmtUSD(account.pnl, 0) }}
              ({{ account.pnl_pct != null ? (account.pnl_pct * 100).toFixed(2) + '%' : '-' }})
            </div>
          </div>
        </n-card>
      </n-gi>
      <n-gi :span="isMobile ? 2 : 4">
        <n-card hoverable>
          <n-space vertical>
            <n-button type="primary" @click="executeTrade" :loading="tradeLoading" :disabled="tradeLoading" style="width: 100%">
              <template #icon><n-icon><PlayOutline /></n-icon></template>
              Execute Trade
            </n-button>
            <n-button type="error" secondary @click="handleReset" style="width: 100%">
              <template #icon><n-icon><TrashOutline /></n-icon></template>
              Reset Account
            </n-button>
          </n-space>
        </n-card>
      </n-gi>
    </n-grid>

    <!-- Positions -->
    <n-card hoverable style="margin-bottom: 20px" title="Current Positions">
      <template #header-extra>
        <n-button text @click="loadAll">
          <template #icon><n-icon><RefreshOutline /></n-icon></template>
          Refresh
        </n-button>
      </template>

      <template v-if="positions.length">
        <!-- Desktop: data table -->
        <n-data-table
          v-if="!isMobile"
          :columns="positionColumns"
          :data="positions"
          :bordered="false"
          size="small"
          :row-key="(row: any) => row.ticker"
        />
        <!-- Mobile: card list -->
        <div v-else class="mobile-positions">
          <n-card v-for="pos in positions" :key="pos.ticker" size="small" style="margin-bottom: 8px">
            <div class="pos-header">
              <span class="pos-ticker">{{ pos.ticker }}</span>
              <span v-if="pos.name" class="pos-name">{{ pos.name }}</span>
              <span class="pos-pnl" :style="{ color: semanticColor(pos.pnl_pct) }">
                {{ pos.pnl_pct != null ? (pos.pnl_pct * 100).toFixed(2) + '%' : '-' }}
              </span>
            </div>
            <div class="pos-details">
              <span>Vol: {{ pos.volume }}</span>
              <span>Cost: {{ fmtUSD(pos.cost_basis) }}</span>
              <span>Price: {{ fmtUSD(pos.current_price) }}</span>
              <span>MV: {{ fmtUSD(pos.market_value, 0) }}</span>
              <span :style="{ color: semanticColor(pos.pnl) }">P&L: {{ fmtUSD(pos.pnl) }}</span>
            </div>
          </n-card>
        </div>
      </template>
      <n-empty v-else description="No positions" />
    </n-card>

    <!-- NAV chart -->
    <n-card hoverable style="margin-bottom: 20px" v-if="navData.length" title="NAV Chart">
      <NavChart :nav="navChartData()" height="300px" />
    </n-card>
  </n-spin>
</template>

<style scoped>
.metric {
  text-align: center;
}
.metric-label {
  font-size: 13px;
  color: v-bind('colors.textTertiary');
  margin-bottom: 8px;
}
.metric-value {
  font-size: 20px;
  font-weight: 600;
  color: v-bind('colors.textPrimary');
}

.mobile-positions .pos-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.mobile-positions .pos-ticker {
  font-weight: 600;
  font-size: 15px;
}
.mobile-positions .pos-name {
  font-size: 12px;
  color: v-bind('colors.textTertiary');
}
.mobile-positions .pos-pnl {
  margin-left: auto;
  font-weight: 600;
}
.mobile-positions .pos-details {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 12px;
  color: v-bind('colors.textSecondary');
}
</style>
