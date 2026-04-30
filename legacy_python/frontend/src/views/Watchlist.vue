<script setup lang="ts">
import { ref, h, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useResponsive } from '../composables/useResponsive'
import { useMessage, NButton, NSpace, NText } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import {
  getWatchlist, addToWatchlist, removeFromWatchlist,
  searchStocks, getStockProfile, getStockKline, getStockReports, getStockNews,
} from '../api'
import KlineChart from '../components/KlineChart.vue'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const { isMobile } = useResponsive()

// ── Watchlist ──
const wlLoading = ref(false)
const wlData = ref<any[]>([])

async function loadWatchlist() {
  wlLoading.value = true
  try {
    const res = await getWatchlist()
    wlData.value = res.data.data || []
  } catch { wlData.value = [] }
  wlLoading.value = false
}

async function handleRemove(code: string, e?: Event) {
  e?.stopPropagation()
  try {
    await removeFromWatchlist(code)
    message.success('已移除')
    wlData.value = wlData.value.filter(d => d.ts_code !== code)
    if (selectedCode.value === code) {
      selectedCode.value = ''
      profile.value = null
    }
  } catch (err: any) { message.error(err.response?.data?.error || '移除失败') }
}

// ── Search & Add ──
const searchQuery = ref('')
const searchOptions = ref<{ label: string; value: string }[]>([])
let searchTimer: ReturnType<typeof setTimeout> | null = null

function handleSearch(value: string) {
  if (searchTimer) clearTimeout(searchTimer)
  if (!value || value.length < 1) { searchOptions.value = []; return }
  searchTimer = setTimeout(async () => {
    try {
      const { data } = await searchStocks(value)
      searchOptions.value = (data.results || []).map((s: any) => ({
        label: `${s.ts_code} - ${s.name}${s.is_st ? ' (ST)' : ''}`,
        value: s.ts_code,
      }))
    } catch { searchOptions.value = [] }
  }, 300)
}

async function handleSearchSelect(code: string) {
  searchQuery.value = ''
  // Add to watchlist if not present
  if (!wlData.value.find(d => d.ts_code === code)) {
    try {
      await addToWatchlist(code)
      message.success('已添加到自选股')
      await loadWatchlist()
    } catch (err: any) { message.error(err.response?.data?.error || '添加失败') }
  }
  selectStock(code)
}

// ── Stock Detail ──
const selectedCode = ref('')
const detailLoading = ref(false)
const profile = ref<any>(null)
const klineData = ref<any[]>([])
const reports = ref<any[]>([])
const reportsTotal = ref(0)
const reportsPage = ref(1)
const newsData = ref<any[]>([])
const newsTotal = ref(0)
const newsPage = ref(1)

function selectStock(code: string) {
  selectedCode.value = code
  router.replace({ query: code ? { code } : {} })
  if (code) loadStockDetail(code)
}

async function loadStockDetail(code: string) {
  detailLoading.value = true
  reportsPage.value = 1
  newsPage.value = 1

  const end = new Date()
  const start = new Date()
  start.setMonth(start.getMonth() - 6)

  const [profileRes, klineRes, reportsRes, newsRes] = await Promise.allSettled([
    getStockProfile(code),
    getStockKline(code, start.toISOString().slice(0, 10), end.toISOString().slice(0, 10)),
    getStockReports(code, 1),
    getStockNews(code, 1),
  ])

  profile.value = profileRes.status === 'fulfilled' ? profileRes.value.data : null
  klineData.value = klineRes.status === 'fulfilled' ? (klineRes.value.data.data || []) : []
  if (reportsRes.status === 'fulfilled') {
    reports.value = reportsRes.value.data.data || []
    reportsTotal.value = reportsRes.value.data.total || 0
  } else { reports.value = []; reportsTotal.value = 0 }
  if (newsRes.status === 'fulfilled') {
    newsData.value = newsRes.value.data.data || []
    newsTotal.value = newsRes.value.data.total || 0
  } else { newsData.value = []; newsTotal.value = 0 }

  detailLoading.value = false
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

// ── Columns ──
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

function wlDesktopColumns(): DataTableColumns {
  return [
    {
      title: '代码', key: 'ts_code', width: 110,
      render: (row: any) => h(NButton, {
        text: true, type: 'primary',
        strong: selectedCode.value === row.ts_code,
        onClick: () => selectStock(row.ts_code),
      }, () => row.ts_code),
    },
    { title: '名称', key: 'name', width: 100, ellipsis: { tooltip: true } },
    {
      title: '最新价', key: 'close', width: 90, align: 'right',
      render: (row: any) => row.close != null ? row.close.toFixed(2) : '-',
    },
    {
      title: '涨跌幅', key: 'pct_chg', width: 90, align: 'right',
      render: (row: any) => {
        if (row.pct_chg == null) return '-'
        const v = Number(row.pct_chg)
        const color = v > 0 ? '#d03050' : v < 0 ? '#18a058' : undefined
        return h(NText, { style: { color } }, () => `${v > 0 ? '+' : ''}${v.toFixed(2)}%`)
      },
    },
    {
      title: '总市值', key: 'total_mv', width: 120, align: 'right',
      render: (row: any) => fmtMv(row.total_mv),
    },
    { title: '数据日期', key: 'trade_date', width: 110 },
    {
      title: '操作', key: 'actions', width: 80, align: 'center',
      render: (row: any) => h(NButton, {
        size: 'small', type: 'error',
        onClick: (e: Event) => handleRemove(row.ts_code, e),
      }, () => '移除'),
    },
  ]
}

function wlMobileColumns(): DataTableColumns {
  return [
    {
      title: '股票', key: 'stock',
      render: (row: any) => h('div', {
        style: `cursor:pointer;${selectedCode.value === row.ts_code ? 'background:#f0f7ff;border-radius:4px;padding:2px 4px;margin:-2px -4px' : ''}`,
        onClick: () => selectStock(row.ts_code),
      }, [
        h('div', { style: 'font-weight:600' }, row.name || row.ts_code),
        h(NText, { depth: 3, style: 'font-size:12px' }, () => row.ts_code),
      ]),
    },
    {
      title: '行情', key: 'close', width: 80, align: 'right',
      render: (row: any) => {
        const price = row.close != null ? row.close.toFixed(2) : '-'
        const pct = row.pct_chg != null ? `${row.pct_chg > 0 ? '+' : ''}${Number(row.pct_chg).toFixed(2)}%` : ''
        const color = row.pct_chg > 0 ? '#d03050' : row.pct_chg < 0 ? '#18a058' : undefined
        return h('div', { style: { textAlign: 'right' } }, [
          h('div', price),
          pct ? h(NText, { style: { color, fontSize: '12px' } }, () => pct) : null,
        ])
      },
    },
    {
      title: '', key: 'actions', width: 50, align: 'center',
      render: (row: any) => h(NButton, {
        size: 'tiny', type: 'error', text: true,
        onClick: (e: Event) => handleRemove(row.ts_code, e),
      }, () => '移除'),
    },
  ]
}

// ── Formatters ──
function fmtMv(val: any) {
  if (val == null) return '-'
  const n = Number(val)
  if (n >= 10000) return (n / 10000).toFixed(2) + ' 亿'
  return n.toFixed(2) + ' 万'
}

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

// ── Init ──
onMounted(async () => {
  await loadWatchlist()
  const code = route.query.code as string
  if (code) selectStock(code)
})
</script>

<template>
  <div>
    <!-- Search + Watchlist -->
    <n-card hoverable :style="{ marginBottom: selectedCode ? '20px' : '0' }">
      <template #header>
        <n-space :align="isMobile ? 'start' : 'center'" :justify="isMobile ? 'start' : 'space-between'" :vertical="isMobile" :size="isMobile ? 8 : 0">
          <span>自选股 ({{ wlData.length }})</span>
          <n-space :size="8">
            <n-auto-complete
              v-model:value="searchQuery"
              :options="searchOptions"
              placeholder="搜索添加股票..."
              :style="{ width: isMobile ? '180px' : '260px' }"
              size="small"
              @update:value="handleSearch"
              @select="handleSearchSelect"
              clearable
            />
            <n-button size="small" @click="loadWatchlist" :loading="wlLoading">刷新</n-button>
          </n-space>
        </n-space>
      </template>

      <n-data-table
        :columns="isMobile ? wlMobileColumns() : wlDesktopColumns()"
        :data="wlData"
        :loading="wlLoading"
        striped
        size="small"
        :bordered="false"
        :row-key="(row: any) => row.ts_code"
        :row-class-name="(row: any) => row.ts_code === selectedCode ? 'row-selected' : ''"
        max-height="360"
      />
      <n-empty v-if="!wlLoading && wlData.length === 0" description="搜索股票代码或名称添加自选" style="padding: 30px 0" />
    </n-card>

    <!-- Stock Detail -->
    <n-spin :show="detailLoading" v-if="selectedCode">
      <n-grid :cols="isMobile ? 1 : 24" :x-gap="isMobile ? 0 : 20">
        <!-- Left: K-line + Reports + News -->
        <n-gi :span="isMobile ? 1 : 16">
          <n-card hoverable style="margin-bottom: 20px">
            <template #header>
              <n-space align="center" :size="12">
                <span>{{ profile ? `${profile.name} (${selectedCode})` : selectedCode }}</span>
                <n-button size="small" type="error" @click="handleRemove(selectedCode)">移除自选</n-button>
              </n-space>
            </template>
            <KlineChart :data="klineData" />
          </n-card>

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

        <!-- Right: Basic Info + Financial -->
        <n-gi :span="isMobile ? 1 : 8">
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
    </n-spin>
  </div>
</template>

<style scoped>
:deep(.row-selected td) {
  background: rgba(24, 160, 88, 0.08) !important;
}
</style>
