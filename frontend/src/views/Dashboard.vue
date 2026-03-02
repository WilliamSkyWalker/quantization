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
</script>

<template>
  <n-spin :show="loading">
    <n-alert v-if="error" type="error" title="数据加载失败" style="margin-bottom: 20px">
      部分数据加载失败，请检查后端服务是否正常运行。
    </n-alert>

    <!-- Stats cards -->
    <n-grid :cols="4" :x-gap="20" style="margin-bottom: 20px">
      <n-gi>
        <n-card hoverable>
          <div class="stat-card">
            <div class="stat-icon" style="background: #ecf5ff">
              <n-icon size="28" color="#409eff"><ListOutline /></n-icon>
            </div>
            <div>
              <div class="stat-value">{{ stats.stockCount.toLocaleString() }}</div>
              <div class="stat-label">股票数量</div>
            </div>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card hoverable>
          <div class="stat-card">
            <div class="stat-icon" style="background: #f0f9eb">
              <n-icon size="28" color="#67c23a"><CalendarOutline /></n-icon>
            </div>
            <div>
              <div class="stat-value">{{ stats.latestDate }}</div>
              <div class="stat-label">最新行情日期</div>
            </div>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card hoverable>
          <div class="stat-card">
            <div class="stat-icon" style="background: #fef0f0">
              <n-icon size="28" color="#f56c6c"><TrendingUpOutline /></n-icon>
            </div>
            <div>
              <div class="stat-value">{{ stats.paperNav ? stats.paperNav.toFixed(4) : '-' }}</div>
              <div class="stat-label">模拟盘净值</div>
            </div>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card hoverable>
          <div class="stat-card">
            <div class="stat-icon" style="background: #fdf6ec">
              <n-icon size="28" color="#e6a23c"><ChatbubblesOutline /></n-icon>
            </div>
            <div>
              <div class="stat-value">{{ stats.articleCount.toLocaleString() }}</div>
              <div class="stat-label">舆情文章数</div>
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
}

.stat-value {
  font-size: 22px;
  font-weight: 600;
  color: #303133;
}

.stat-label {
  font-size: 13px;
  color: #909399;
  margin-top: 4px;
}
</style>
