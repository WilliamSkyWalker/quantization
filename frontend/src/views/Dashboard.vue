<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { NIcon } from 'naive-ui'
import {
  ListOutline,
  CalendarOutline,
  TrendingUpOutline,
  ChatbubblesOutline,
} from '@vicons/ionicons5'
import { getDataStatus, getPaperAccount, getPaperNav, getSentimentStatus } from '../api'
import { colors } from '../theme'
import NavChart from '../components/NavChart.vue'

const loading = ref(true)
const error = ref(false)
const stats = ref({
  stockCount: 0,
  latestDate: '-',
  paperNav: 0,
  articleCount: 0,
})
const paperNavData = ref<{ date: string; nav: number }[]>([])

onMounted(async () => {
  loading.value = true
  error.value = false
  try {
    const [dataRes, accountRes, navRes, sentRes] = await Promise.allSettled([
      getDataStatus(),
      getPaperAccount(),
      getPaperNav(30),
      getSentimentStatus(),
    ])

    const allFailed =
      dataRes.status === 'rejected' &&
      accountRes.status === 'rejected' &&
      navRes.status === 'rejected' &&
      sentRes.status === 'rejected'

    if (allFailed) {
      error.value = true
    }

    if (dataRes.status === 'fulfilled') {
      const tables = dataRes.value.data.tables
      const stockBasic = tables.find((t: any) => t.table === 'stock_basic')
      stats.value.stockCount = stockBasic?.count || 0
      stats.value.latestDate = dataRes.value.data.latest_trade_date || '-'
    }

    if (accountRes.status === 'fulfilled') {
      const nav = accountRes.value.data.total_assets / accountRes.value.data.initial_capital
      stats.value.paperNav = nav
    }

    if (navRes.status === 'fulfilled') {
      paperNavData.value = navRes.value.data || []
    }

    if (sentRes.status === 'fulfilled') {
      stats.value.articleCount = sentRes.value.data.total || 0
    }
  } finally {
    loading.value = false
  }
})

const statCards = [
  { key: 'stockCount', label: '股票数量', icon: ListOutline, bg: colors.statBlue, fg: colors.primary },
  { key: 'latestDate', label: '最新行情日期', icon: CalendarOutline, bg: colors.statGreen, fg: colors.success },
  { key: 'paperNav', label: '模拟盘净值', icon: TrendingUpOutline, bg: colors.statRed, fg: colors.error },
  { key: 'articleCount', label: '舆情文章数', icon: ChatbubblesOutline, bg: colors.statOrange, fg: colors.warning },
]

function formatStat(key: string) {
  const v = (stats.value as any)[key]
  if (key === 'paperNav') return v ? v.toFixed(4) : '-'
  if (typeof v === 'number') return v.toLocaleString()
  return v || '-'
}
</script>

<template>
  <n-spin :show="loading">
    <n-alert v-if="error" type="error" title="数据加载失败" style="margin-bottom: 20px">
      部分数据加载失败，请检查后端服务是否正常运行。
    </n-alert>

    <!-- Stats cards -->
    <n-grid :cols="4" :x-gap="16" style="margin-bottom: 20px">
      <n-gi v-for="card in statCards" :key="card.key">
        <n-card hoverable>
          <div class="stat-card">
            <div class="stat-icon" :style="{ background: card.bg }">
              <n-icon size="28" :color="card.fg"><component :is="card.icon" /></n-icon>
            </div>
            <div>
              <div class="stat-value">{{ formatStat(card.key) }}</div>
              <div class="stat-label">{{ card.label }}</div>
            </div>
          </div>
        </n-card>
      </n-gi>
    </n-grid>

    <!-- Paper trading NAV chart -->
    <n-card hoverable v-if="paperNavData.length > 0" title="模拟盘净值曲线 (近30天)">
      <NavChart :nav="paperNavData" height="300px" />
    </n-card>

    <n-card hoverable v-else title="模拟盘净值曲线">
      <n-empty description="暂无净值数据，请先运行模拟交易" />
    </n-card>
  </n-spin>
</template>

<style scoped>
.stat-card {
  display: flex;
  align-items: center;
  gap: 16px;
}

.stat-icon {
  width: 56px;
  height: 56px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.stat-value {
  font-size: 22px;
  font-weight: 600;
  color: v-bind('colors.textPrimary');
}

.stat-label {
  font-size: 13px;
  color: v-bind('colors.textTertiary');
  margin-top: 4px;
}
</style>
