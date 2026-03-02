<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useMessage, useDialog, NIcon } from 'naive-ui'
import { PlayOutline, TrashOutline, RefreshOutline } from '@vicons/ionicons5'
import { getPaperAccount, getPaperPositions, getPaperNav, getPaperTransactions, startPaperTrade, resetPaper } from '../api'
import { useTaskStore } from '../stores/task'
import NavChart from '../components/NavChart.vue'
import PositionTable from '../components/PositionTable.vue'
import TradeLog from '../components/TradeLog.vue'

const message = useMessage()
const dialogCtrl = useDialog()
const taskStore = useTaskStore()
const loading = ref(false)
const tradeLoading = ref(false)
const account = ref<any>(null)
const positions = ref<any[]>([])
const navData = ref<any[]>([])
const transactions = ref<any[]>([])

async function loadAll() {
  loading.value = true
  try {
    const [acctRes, posRes, navRes, txRes] = await Promise.allSettled([
      getPaperAccount(),
      getPaperPositions(),
      getPaperNav(60),
      getPaperTransactions(30),
    ])
    if (acctRes.status === 'fulfilled') account.value = acctRes.value.data
    if (posRes.status === 'fulfilled') positions.value = posRes.value.data
    if (navRes.status === 'fulfilled') navData.value = navRes.value.data
    if (txRes.status === 'fulfilled') transactions.value = txRes.value.data
  } finally {
    loading.value = false
  }
}

async function executeTrade() {
  tradeLoading.value = true
  try {
    const { data } = await startPaperTrade()
    taskStore.trackTask(data.task_id, '执行交易信号')
    message.success('交易任务已启动')
  } catch (e: any) {
    message.error('操作失败')
  } finally {
    tradeLoading.value = false
  }
}

function doReset() {
  dialogCtrl.warning({
    title: '确认重置',
    content: '确定要重置模拟账户吗？所有持仓和交易记录将被清除。',
    positiveText: '确认',
    negativeText: '取消',
    onPositiveClick: async () => {
      await resetPaper()
      message.success('模拟账户已重置')
      await loadAll()
    },
  })
}

onMounted(loadAll)
</script>

<template>
  <n-spin :show="loading">
    <!-- Account overview -->
    <n-grid :cols="24" :x-gap="16" style="margin-bottom: 20px" v-if="account">
      <n-gi :span="5">
        <n-card hoverable>
          <div class="metric">
            <div class="metric-label">初始资金</div>
            <div class="metric-value">{{ account.initial_capital?.toLocaleString() }}</div>
          </div>
        </n-card>
      </n-gi>
      <n-gi :span="5">
        <n-card hoverable>
          <div class="metric">
            <div class="metric-label">现金</div>
            <div class="metric-value">{{ account.cash?.toLocaleString(undefined, { maximumFractionDigits: 0 }) }}</div>
          </div>
        </n-card>
      </n-gi>
      <n-gi :span="5">
        <n-card hoverable>
          <div class="metric">
            <div class="metric-label">总资产</div>
            <div class="metric-value">{{ account.total_assets?.toLocaleString(undefined, { maximumFractionDigits: 0 }) }}</div>
          </div>
        </n-card>
      </n-gi>
      <n-gi :span="5">
        <n-card hoverable>
          <div class="metric">
            <div class="metric-label">盈亏</div>
            <div class="metric-value" :style="{ color: account.pnl > 0 ? '#f56c6c' : account.pnl < 0 ? '#67c23a' : '#999' }">
              {{ account.pnl?.toLocaleString(undefined, { maximumFractionDigits: 0 }) }}
              ({{ account.pnl_pct != null ? (account.pnl_pct * 100).toFixed(2) + '%' : '-' }})
            </div>
          </div>
        </n-card>
      </n-gi>
      <n-gi :span="4">
        <n-card hoverable>
          <n-space vertical>
            <n-button type="primary" @click="executeTrade" :loading="tradeLoading" :disabled="tradeLoading" style="width: 100%">
              <template #icon><n-icon><PlayOutline /></n-icon></template>
              执行今日交易
            </n-button>
            <n-button type="error" secondary @click="doReset" style="width: 100%">
              <template #icon><n-icon><TrashOutline /></n-icon></template>
              重置账户
            </n-button>
          </n-space>
        </n-card>
      </n-gi>
    </n-grid>

    <!-- Positions -->
    <n-card hoverable style="margin-bottom: 20px" title="当前持仓">
      <PositionTable :positions="positions" v-if="positions.length" />
      <n-empty v-else description="暂无持仓" />
    </n-card>

    <!-- NAV chart -->
    <n-card hoverable style="margin-bottom: 20px" v-if="navData.length" title="净值曲线">
      <NavChart :nav="navData" height="300px" />
    </n-card>

    <!-- Recent trades -->
    <n-card hoverable>
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center; width: 100%">
          <span>最近交易</span>
          <n-button text @click="loadAll">
            <template #icon><n-icon><RefreshOutline /></n-icon></template>
            刷新
          </n-button>
        </div>
      </template>
      <TradeLog :trades="transactions" v-if="transactions.length" />
      <n-empty v-else description="暂无交易记录" />
    </n-card>
  </n-spin>
</template>

<style scoped>
.metric {
  text-align: center;
}
.metric-label {
  font-size: 13px;
  color: #909399;
  margin-bottom: 8px;
}
.metric-value {
  font-size: 20px;
  font-weight: 600;
  color: #303133;
}
</style>
