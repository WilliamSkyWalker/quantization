<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useResponsive } from '../composables/useResponsive'
import type { DataTableColumns } from 'naive-ui'
import { searchStocks, getStockProfile, getStockKline, getStockReports, getStockNews, checkWatchlist, addToWatchlist, removeFromWatchlist } from '../api'
import { useMessage } from 'naive-ui'
import KlineChart from '../components/KlineChart.vue'

const route = useRoute()
const router = useRouter()
const { isMobile } = useResponsive()

const searchQuery = ref('')
const searchOptions = ref<{ label: string; value: string }[]>([])
const selectedCode = ref('')
const loading = ref(false)
const message = useMessage()
const inWatchlist = ref(false)

const profile = ref<any>(null)
const klineData = ref<any[]>([])
const reports = ref<any[]>([])
const reportsTotal = ref(0)
const reportsPage = ref(1)
const newsData = ref<any[]>([])
const newsTotal = ref(0)
const newsPage = ref(1)

let searchTimer: ReturnType<typeof setTimeout> | null = null

function handleSearch(value: string) {
  if (searchTimer) clearTimeout(searchTimer)
  if (!value || value.length < 1) {
    searchOptions.value = []
    return
  }
  searchTimer = setTimeout(async () => {
    try {
      const { data } = await searchStocks(value)
      searchOptions.value = (data.results || []).map((s: any) => ({
        label: `${s.ts_code} - ${s.name}${s.is_st ? ' (ST)' : ''}`,
        value: s.ts_code,
      }))
    } catch {
      searchOptions.value = []
    }
  }, 300)
}

function handleSelect(code: string) {
  selectedCode.value = code
  searchQuery.value = code
  router.replace({ query: { code } })
  loadStockData(code)
}

async function loadStockData(code: string) {
  loading.value = true
  reportsPage.value = 1
  newsPage.value = 1

  // Default 6 months kline
  const end = new Date()
  const start = new Date()
  start.setMonth(start.getMonth() - 6)
  const startDate = start.toISOString().slice(0, 10)
  const endDate = end.toISOString().slice(0, 10)

  const [profileRes, klineRes, reportsRes, newsRes, wlRes] = await Promise.allSettled([
    getStockProfile(code),
    getStockKline(code, startDate, endDate),
    getStockReports(code, 1),
    getStockNews(code, 1),
    checkWatchlist(code),
  ])

  profile.value = profileRes.status === 'fulfilled' ? profileRes.value.data : null
  klineData.value = klineRes.status === 'fulfilled' ? (klineRes.value.data.data || []) : []
  if (reportsRes.status === 'fulfilled') {
    reports.value = reportsRes.value.data.data || []
    reportsTotal.value = reportsRes.value.data.total || 0
  } else {
    reports.value = []
    reportsTotal.value = 0
  }
  if (newsRes.status === 'fulfilled') {
    newsData.value = newsRes.value.data.data || []
    newsTotal.value = newsRes.value.data.total || 0
  } else {
    newsData.value = []
    newsTotal.value = 0
  }

  inWatchlist.value = wlRes.status === 'fulfilled' ? wlRes.value.data.in_watchlist : false

  loading.value = false
}

async function toggleWatchlist() {
  if (!selectedCode.value) return
  try {
    if (inWatchlist.value) {
      await removeFromWatchlist(selectedCode.value)
      inWatchlist.value = false
      message.success('已从自选股移除')
    } else {
      await addToWatchlist(selectedCode.value)
      inWatchlist.value = true
      message.success('已添加到自选股')
    }
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  }
}

async function loadReportsPage(page: number) {
  reportsPage.value = page
  try {
    const { data } = await getStockReports(selectedCode.value, page)
    reports.value = data.data || []
    reportsTotal.value = data.total || 0
  } catch { /* ignore */ }
}

async function loadNewsPage(page: number) {
  newsPage.value = page
  try {
    const { data } = await getStockNews(selectedCode.value, page)
    newsData.value = data.data || []
    newsTotal.value = data.total || 0
  } catch { /* ignore */ }
}

const reportColumns: DataTableColumns = [
  { title: '机构', key: 'institution', width: 120, ellipsis: { tooltip: true } },
  { title: '分析师', key: 'analyst', width: 100, ellipsis: { tooltip: true } },
  { title: '标题', key: 'title', ellipsis: { tooltip: true } },
  { title: '评级', key: 'rating', width: 80 },
  { title: '日期', key: 'report_date', width: 110 },
]

const newsColumns: DataTableColumns = [
  { title: '来源', key: 'source', width: 80 },
  { title: '标题', key: 'title', ellipsis: { tooltip: true } },
  { title: '日期', key: 'publish_date', width: 110 },
  {
    title: '情感', key: 'sentiment', width: 80,
    render: (row: any) => row.sentiment != null ? row.sentiment.toFixed(2) : '-',
  },
  { title: '类型', key: 'impact_type', width: 100, ellipsis: { tooltip: true } },
]

function fmtMv(val: any) {
  if (val == null) return '-'
  const n = Number(val)
  if (n >= 10000) return (n / 10000).toFixed(2) + ' 亿'
  return n.toFixed(2) + ' 万'
}

/** 元 → 万/亿 显示 */
function fmtYuan(val: any) {
  if (val == null) return '-'
  const wan = Number(val) / 10000
  if (wan >= 10000) return (wan / 10000).toFixed(2) + ' 亿'
  return wan.toFixed(2) + ' 万'
}

function fmtPct(val: any) {
  if (val == null) return '-'
  return Number(val).toFixed(2) + '%'
}

function fmtNum(val: any, digits = 2) {
  if (val == null) return '-'
  return Number(val).toFixed(digits)
}

onMounted(() => {
  const code = route.query.code as string
  if (code) {
    selectedCode.value = code
    searchQuery.value = code
    loadStockData(code)
  }
})
</script>

<template>
  <div>
    <!-- Search -->
    <n-card hoverable style="margin-bottom: 20px">
      <n-space align="center">
        <n-auto-complete
          v-model:value="searchQuery"
          :options="searchOptions"
          placeholder="输入股票代码或名称搜索..."
          :loading="false"
          :style="{ width: isMobile ? '100%' : '400px' }"
          @update:value="handleSearch"
          @select="handleSelect"
          clearable
        />
      </n-space>
    </n-card>

    <n-spin :show="loading">
      <template v-if="selectedCode">
        <n-grid :cols="isMobile ? 1 : 24" :x-gap="isMobile ? 0 : 20">
          <!-- Left column -->
          <n-gi :span="isMobile ? 1 : 16">
            <!-- K-line -->
            <n-card hoverable style="margin-bottom: 20px">
              <template #header>
                <n-space align="center" :size="12">
                  <span>{{ profile ? `${profile.name} (${selectedCode})` : selectedCode }}</span>
                  <n-button
                    :type="inWatchlist ? 'warning' : 'default'"
                    size="small"
                    @click="toggleWatchlist"
                  >
                    {{ inWatchlist ? '已自选' : '+ 自选' }}
                  </n-button>
                </n-space>
              </template>
              <KlineChart :data="klineData" />
            </n-card>

            <!-- Research Reports -->
            <n-card hoverable style="margin-bottom: 20px">
              <template #header>券商研报 ({{ reportsTotal }})</template>
              <n-data-table :columns="reportColumns" :data="reports" striped size="small" :bordered="false" />
              <n-pagination
                v-if="reportsTotal > 20"
                :page="reportsPage"
                :page-count="Math.ceil(reportsTotal / 20)"
                @update:page="loadReportsPage"
                style="margin-top: 12px; justify-content: flex-end"
              />
            </n-card>

            <!-- News -->
            <n-card hoverable>
              <template #header>相关舆情 ({{ newsTotal }})</template>
              <n-data-table :columns="newsColumns" :data="newsData" striped size="small" :bordered="false" />
              <n-pagination
                v-if="newsTotal > 20"
                :page="newsPage"
                :page-count="Math.ceil(newsTotal / 20)"
                @update:page="loadNewsPage"
                style="margin-top: 12px; justify-content: flex-end"
              />
            </n-card>
          </n-gi>

          <!-- Right column -->
          <n-gi :span="isMobile ? 1 : 8">
            <!-- Basic Info -->
            <n-card hoverable style="margin-bottom: 20px" title="基本信息" v-if="profile">
              <n-descriptions :column="1" label-placement="left" size="small" bordered>
                <n-descriptions-item label="代码">{{ profile.ts_code }}</n-descriptions-item>
                <n-descriptions-item label="名称">{{ profile.name }}</n-descriptions-item>
                <n-descriptions-item label="市场">{{ profile.market }}</n-descriptions-item>
                <n-descriptions-item label="上市日期">{{ profile.list_date }}</n-descriptions-item>
                <n-descriptions-item label="总股本">{{ fmtMv(profile.total_share) }}</n-descriptions-item>
                <n-descriptions-item label="流通股">{{ fmtMv(profile.float_share) }}</n-descriptions-item>
                <n-descriptions-item label="是否ST">{{ profile.is_st ? '是' : '否' }}</n-descriptions-item>
                <n-descriptions-item label="行业">{{ profile.industry_name || '-' }}</n-descriptions-item>
                <n-descriptions-item label="二级行业">{{ profile.l2_industry_name || '-' }}</n-descriptions-item>
              </n-descriptions>
            </n-card>

            <!-- Financial Info -->
            <n-card hoverable title="财务信息" v-if="profile">
              <n-descriptions :column="1" label-placement="left" size="small" bordered>
                <n-descriptions-item label="PE(TTM)">{{ fmtNum(profile.pe_ttm) }}</n-descriptions-item>
                <n-descriptions-item label="PB">{{ fmtNum(profile.pb) }}</n-descriptions-item>
                <n-descriptions-item label="ROE(TTM)">{{ fmtPct(profile.roe_ttm) }}</n-descriptions-item>
                <n-descriptions-item label="毛利率">{{ fmtPct(profile.gross_margin) }}</n-descriptions-item>
                <n-descriptions-item label="营收">{{ fmtYuan(profile.revenue) }}</n-descriptions-item>
                <n-descriptions-item label="净利润">{{ fmtYuan(profile.net_profit) }}</n-descriptions-item>
                <n-descriptions-item label="总市值">{{ fmtMv(profile.total_mv) }}</n-descriptions-item>
              </n-descriptions>
            </n-card>
          </n-gi>
        </n-grid>
      </template>

      <n-empty v-else description="输入股票代码或名称搜索个股" />
    </n-spin>
  </div>
</template>
