<script setup lang="ts">
import { h, ref, onMounted, computed } from 'vue'
import { useMessage, NTag, NIcon, NButton, NSpace, NDivider } from 'naive-ui'
import { RefreshOutline, SearchOutline, OpenOutline, DownloadOutline } from '@vicons/ionicons5'
import type { DataTableColumns } from 'naive-ui'
import { getSentimentStatus, getSentimentArticles, getSentimentAnalysisStats, startSentimentDownload, startSentimentDownloadAndAnalyze, startSentimentBackfillAnalyze, startSentimentBackfillContent, startDownload } from '../api'
import { formatDate } from '../utils/format'
import { useTaskStore } from '../stores/task'

const message = useMessage()
const taskStore = useTaskStore()
const loading = ref(false)
const scraping = ref(false)
const sources = ref<any[]>([])
const total = ref(0)

// Tab state
const activeTab = ref('overview')

// Article list state
const articles = ref<any[]>([])
const articleTotal = ref(0)
const currentPage = ref(1)
const articleLoading = ref(false)

// Filters
const currentSource = ref('')
const currentCategory = ref<string | null>(null)
const dateRange = ref<[number, number] | null>(null)
const keyword = ref('')

// Category options from backend
const categories = ref<string[]>([])

// Analysis stats
const analysisStats = ref({ keyword_analyzed: 0, llm_analyzed: 0, pending_keyword: 0, llm_available: false })

// Drawer state
const drawerVisible = ref(false)
const selectedArticle = ref<any>(null)

const tierNames: Record<number, string> = {
  1: '最高层', 2: '产业层', 3: '金融监管', 4: '专项行业', 5: '美国政策', 6: '券商研报',
}

const tierColors: Record<number, string> = {
  1: '#f56c6c', 2: '#e6a23c', 3: '#409eff', 4: '#67c23a', 5: '#909399', 6: '#8b5cf6',
}

// Source table columns
const sourceColumns: DataTableColumns = [
  {
    title: '层级', key: 'tier', width: 110,
    render: (row: any) => h(NTag, {
      color: { color: tierColors[row.tier], textColor: '#fff' },
      size: 'small',
    }, { default: () => `T${row.tier} ${tierNames[row.tier] || ''}` }),
  },
  { title: '来源', key: 'label', width: 130 },
  {
    title: '篇数', key: 'count', width: 80, align: 'right',
    render: (row: any) => h('span', {
      style: row.count > 0 ? 'font-weight: 600' : 'color: #c0c4cc',
    }, row.count?.toLocaleString() ?? '0'),
  },
  { title: '最早', key: 'earliest', width: 110, render: (row: any) => row.earliest || '-' },
  { title: '最新', key: 'latest', width: 110, render: (row: any) => row.latest || '-' },
  {
    title: '操作', key: 'actions', width: 200,
    render: (row: any) => h(NSpace, { size: 'small' }, {
      default: () => [
        h(NButton, {
          size: 'tiny', type: 'primary', disabled: scraping.value,
          onClick: () => scrapeSource(row.source),
        }, { default: () => '抓取' }),
        h(NButton, {
          size: 'tiny', disabled: row.count === 0,
          onClick: () => filterBySource(row.source),
        }, { default: () => '查看' }),
      ],
    }),
  },
]

// Sorted sources for table (by tier then source name)
const sortedSources = computed(() =>
  [...sources.value].sort((a, b) => a.tier - b.tier || a.source.localeCompare(b.source))
)

async function scrapeSource(source: string) {
  scraping.value = true
  try {
    if (source === 'research_report') {
      const { data } = await startDownload('update_reports')
      taskStore.trackTask(data.task_id, '刷新券商研报')
      message.success('券商研报刷新任务已启动')
    } else {
      const { data } = await startSentimentDownload(source)
      taskStore.trackTask(data.task_id, `抓取 ${source}`)
      message.success(`${source} 抓取任务已启动`)
    }
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    scraping.value = false
  }
}

async function scrapeAll() {
  scraping.value = true
  try {
    const { data } = await startSentimentDownloadAndAnalyze()
    taskStore.trackTask(data.task_id, '全量抓取+分析')
    message.success('全量抓取+分析任务已启动')
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    scraping.value = false
  }
}

async function backfillAnalyze() {
  scraping.value = true
  try {
    const { data } = await startSentimentBackfillAnalyze()
    taskStore.trackTask(data.task_id, '补录分析')
    message.success('补录分析任务已启动')
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    scraping.value = false
  }
}

async function backfillContent() {
  scraping.value = true
  try {
    const { data } = await startSentimentBackfillContent()
    taskStore.trackTask(data.task_id, '补录全文')
    message.success('补录全文任务已启动')
  } catch (e: any) {
    message.error(e.response?.data?.error || '操作失败')
  } finally {
    scraping.value = false
  }
}

const articleColumns: DataTableColumns = [
  {
    title: '来源', key: 'source', width: 120,
    render: (row: any) => h(
      NTag,
      { type: row.tier <= 2 ? 'error' : row.tier <= 3 ? 'warning' : 'default', size: 'small' },
      { default: () => row.source }
    ),
  },
  {
    title: '栏目', key: 'category', width: 100,
    ellipsis: { tooltip: true },
    render: (row: any) => h('span', { style: 'color: #909399; font-size: 12px' }, row.category || '-'),
  },
  {
    title: '标题', key: 'title', minWidth: 300,
    ellipsis: { tooltip: true },
    render: (row: any) => h('span', { style: 'color: #303133; cursor: pointer' }, row.title),
  },
  { title: '发布日期', key: 'publish_date', width: 110, sorter: 'default' },
  { title: '抓取时间', key: 'scraped_at', width: 170 },
]

async function loadStatus() {
  loading.value = true
  try {
    const { data } = await getSentimentStatus()
    sources.value = data.sources
    total.value = data.total
    categories.value = data.categories || []
  } finally {
    loading.value = false
  }
}

async function loadArticles(page = 1) {
  articleLoading.value = true
  currentPage.value = page
  try {
    const params: Record<string, any> = { page }
    if (currentSource.value) params.source = currentSource.value
    if (currentCategory.value) params.category = currentCategory.value
    if (dateRange.value?.[0]) {
      params.start_date = formatDate(dateRange.value[0])
    }
    if (dateRange.value?.[1]) {
      params.end_date = formatDate(dateRange.value[1])
    }
    if (keyword.value.trim()) params.keyword = keyword.value.trim()

    const { data } = await getSentimentArticles(params)
    articles.value = data.articles
    articleTotal.value = data.total
  } finally {
    articleLoading.value = false
  }
}

function filterBySource(source: string) {
  if (currentSource.value === source) {
    currentSource.value = ''
  } else {
    currentSource.value = source
  }
  activeTab.value = 'articles'
  loadArticles(1)
}

function clearFilters() {
  currentSource.value = ''
  currentCategory.value = null
  dateRange.value = null
  keyword.value = ''
  loadArticles(1)
}

function onSearch() {
  loadArticles(1)
}

function openArticle(row: any) {
  selectedArticle.value = row
  drawerVisible.value = true
}

function rowProps(row: any) {
  return {
    style: 'cursor: pointer',
    onClick: () => openArticle(row),
  }
}

function getTierTagType(tier: number): 'error' | 'warning' | 'info' | 'success' | 'default' {
  if (tier <= 2) return 'error'
  if (tier === 3) return 'warning'
  if (tier === 4) return 'success'
  if (tier === 6) return 'info'
  return 'default'
}

async function loadAnalysisStats() {
  try {
    const { data } = await getSentimentAnalysisStats()
    analysisStats.value = data
  } catch {}
}

function onTabChange(tab: string) {
  activeTab.value = tab
  if (tab === 'articles') {
    loadArticles(currentPage.value)
  }
}

onMounted(() => {
  loadStatus()
  loadArticles()
  loadAnalysisStats()
})
</script>

<template>
  <div>
    <n-tabs type="line" :value="activeTab" @update:value="onTabChange" style="margin-bottom: 16px">
      <n-tab name="overview">数据源概览</n-tab>
      <n-tab name="articles">文章列表</n-tab>
    </n-tabs>

    <!-- Tab: 数据源概览 -->
    <div v-show="activeTab === 'overview'">
      <!-- Action bar -->
      <n-card hoverable style="margin-bottom: 20px">
        <div style="display: flex; justify-content: space-between; align-items: center">
          <n-space size="small" align="center">
            <n-button type="primary" size="small" :disabled="scraping" @click="scrapeAll">
              <template #icon><n-icon><DownloadOutline /></n-icon></template>
              全量抓取+分析
            </n-button>
            <n-button size="small" :disabled="scraping" @click="backfillAnalyze">补录分析</n-button>
            <n-button size="small" :disabled="scraping" @click="backfillContent">补录全文</n-button>
            <n-divider vertical />
            <span style="color: #606266; font-weight: 600">共 {{ total }} 篇</span>
            <n-tag type="info" size="small">关键词已分析: {{ analysisStats.keyword_analyzed }}</n-tag>
            <n-tag type="success" size="small">LLM 已分析: {{ analysisStats.llm_analyzed }}</n-tag>
            <n-tag type="warning" size="small">待分析: {{ analysisStats.pending_keyword }}</n-tag>
            <n-tag :type="analysisStats.llm_available ? 'success' : 'default'" size="small">
              LLM: {{ analysisStats.llm_available ? '可用' : '未配置' }}
            </n-tag>
          </n-space>
          <n-button @click="loadStatus" :loading="loading" size="small">
            <template #icon><n-icon><RefreshOutline /></n-icon></template>
            刷新
          </n-button>
        </div>
      </n-card>

      <!-- Source table -->
      <n-card hoverable>
        <n-data-table
          :columns="sourceColumns"
          :data="sortedSources"
          :loading="loading"
          :row-key="(row: any) => row.source"
          striped
          size="small"
        />
      </n-card>
    </div>

    <!-- Tab: 文章列表 -->
    <div v-show="activeTab === 'articles'">
      <!-- Article filter bar -->
      <n-card hoverable style="margin-bottom: 20px">
        <n-space>
          <n-input
            v-model:value="keyword"
            placeholder="搜索标题关键词"
            clearable
            style="width: 220px"
            @keyup.enter="onSearch"
            @clear="onSearch"
          >
            <template #prefix><n-icon><SearchOutline /></n-icon></template>
          </n-input>
          <n-select
            v-model:value="currentCategory"
            :options="categories.map(c => ({ label: c, value: c }))"
            placeholder="栏目分类"
            clearable
            style="width: 160px"
            @update:value="onSearch"
          />
          <n-date-picker
            type="daterange"
            v-model:value="dateRange"
            clearable
            @update:value="onSearch"
            style="width: 280px"
          />
          <n-button type="primary" @click="onSearch">查询</n-button>
          <n-button @click="clearFilters">重置</n-button>
        </n-space>

        <!-- Active filter tags -->
        <div v-if="currentSource || currentCategory || keyword || dateRange" style="margin-top: 10px">
          <span style="font-size: 12px; color: #909399; margin-right: 8px">筛选条件:</span>
          <n-tag
            v-if="currentSource"
            size="small"
            closable
            @close="currentSource = ''; loadArticles(1)"
            style="margin-right: 6px"
          >
            来源: {{ currentSource }}
          </n-tag>
          <n-tag
            v-if="currentCategory"
            size="small"
            closable
            @close="currentCategory = null; loadArticles(1)"
            style="margin-right: 6px"
          >
            栏目: {{ currentCategory }}
          </n-tag>
          <n-tag
            v-if="keyword"
            size="small"
            closable
            @close="keyword = ''; loadArticles(1)"
            style="margin-right: 6px"
          >
            关键词: {{ keyword }}
          </n-tag>
          <n-tag
            v-if="dateRange"
            size="small"
            closable
            @close="dateRange = null; loadArticles(1)"
          >
            日期范围已选
          </n-tag>
        </div>
      </n-card>

      <!-- Articles table -->
      <n-card hoverable>
        <template #header>
          <div style="display: flex; justify-content: space-between; align-items: center; width: 100%">
            <span>
              文章列表
              <span style="font-size: 13px; color: #909399; margin-left: 8px">
                共 {{ articleTotal }} 篇
              </span>
            </span>
            <n-button text @click="loadArticles(currentPage)">
              <template #icon><n-icon><RefreshOutline /></n-icon></template>
              刷新
            </n-button>
          </div>
        </template>

        <n-data-table
          :columns="articleColumns"
          :data="articles"
          :loading="articleLoading"
          :row-props="rowProps"
          striped
          size="small"
          :row-key="(row: any) => row.url"
          style="width: 100%"
        />

        <div style="margin-top: 12px; text-align: right">
          <n-pagination
            v-model:page="currentPage"
            :item-count="articleTotal"
            :page-size="20"
            show-quick-jumper
            @update:page="loadArticles"
          />
        </div>
      </n-card>
    </div>

    <!-- Article detail drawer -->
    <n-drawer v-model:show="drawerVisible" :width="520" placement="right">
      <n-drawer-content v-if="selectedArticle" closable>
        <template #header>文章详情</template>

        <!-- Title -->
        <h2 style="margin: 0 0 16px 0; font-size: 20px; line-height: 1.4; color: #303133">
          {{ selectedArticle.title }}
        </h2>

        <!-- Meta info -->
        <n-space style="margin-bottom: 16px" align="center">
          <n-tag
            :type="getTierTagType(selectedArticle.tier)"
            size="small"
          >
            {{ selectedArticle.source }}
          </n-tag>
          <span v-if="selectedArticle.category" style="font-size: 13px; color: #909399">
            {{ selectedArticle.category }}
          </span>
          <span style="font-size: 13px; color: #909399">
            {{ selectedArticle.publish_date }}
          </span>
        </n-space>

        <n-divider style="margin: 12px 0" />

        <!-- Summary -->
        <div v-if="selectedArticle.summary" style="margin-bottom: 20px">
          <div style="font-size: 13px; font-weight: 600; color: #606266; margin-bottom: 8px">摘要</div>
          <div class="drawer-summary">
            {{ selectedArticle.summary }}
          </div>
        </div>
        <div v-else style="margin-bottom: 20px; color: #909399; font-size: 13px">
          暂无摘要内容
        </div>

        <!-- View original button -->
        <n-button
          v-if="selectedArticle.url"
          type="primary"
          tag="a"
          :href="selectedArticle.url"
          target="_blank"
          style="margin-bottom: 16px"
        >
          <template #icon><n-icon><OpenOutline /></n-icon></template>
          查看原文
        </n-button>

        <n-divider style="margin: 12px 0" />

        <!-- Scraped time -->
        <div style="font-size: 12px; color: #909399">
          抓取时间：{{ selectedArticle.scraped_at || '-' }}
        </div>
      </n-drawer-content>
    </n-drawer>
  </div>
</template>

<style scoped>
.drawer-summary {
  padding: 12px 16px;
  background: #f5f7fa;
  border-radius: 6px;
  font-size: 14px;
  color: #606266;
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
