<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useMessage } from 'naive-ui'
import { startSelectStocks, getFactorDetail, getSelectHistory, getSelectHistoryDate, getDataStatus } from '../api'
import { useTaskStore } from '../stores/task'
import { formatDate, todayStr } from '../utils/format'
import StockTable from '../components/StockTable.vue'
import { colors, pnlColor } from '../theme'

const message = useMessage()
const taskStore = useTaskStore()
const loading = ref(false)
const progress = ref(0)
const progressMsg = ref('')
const date = ref('')
const result = ref<any>(null)
const resultSource = ref<'live' | 'saved' | null>(null)
const fallbackNotice = ref('')
const selectedStock = ref<any>(null)
const factorLoading = ref(false)
const factorDetail = ref<any>(null)
const historyDates = ref<{ date: string; total: number; updated_at: string | null }[]>([])
let taskId = ''
let taskWatcher: (() => void) | null = null

function stopWatchingTask() {
  if (taskWatcher) {
    taskWatcher()
    taskWatcher = null
  }
}

function startWatchingTask(id: string) {
  stopWatchingTask()
  taskWatcher = watch(
    () => taskStore.tasks[id],
    (task) => {
      if (!task) return
      progress.value = task.progress ?? 0
      progressMsg.value = task.message || '计算中...'
      if (task.status === 'completed') {
        result.value = task.result
        resultSource.value = 'live'
        loading.value = false
        stopWatchingTask()
        loadHistory()
      } else if (task.status === 'failed' || task.status === 'cancelled') {
        message.error(`选股失败: ${task.error || '已取消'}`)
        loading.value = false
        stopWatchingTask()
      }
    },
    { immediate: true },
  )
}

// todayStr imported from utils/format

const historyOptions = computed(() =>
  historyDates.value.map(h => ({
    label: `${h.date}（${h.total} 只）`,
    value: h.date,
  }))
)

const selectedDateInHistory = computed(() =>
  historyDates.value.some(h => h.date === date.value)
)

async function loadHistory() {
  try {
    const { data } = await getSelectHistory()
    historyDates.value = data.dates || []
  } catch { /* ignore */ }
}

async function loadSavedResult(d: string) {
  try {
    const { data } = await getSelectHistoryDate(d)
    result.value = data
    resultSource.value = 'saved'
    factorDetail.value = null
    selectedStock.value = null
  } catch {
    result.value = null
    resultSource.value = null
  }
}

async function onDateChange(d: string) {
  date.value = d
  result.value = null
  resultSource.value = null
  factorDetail.value = null
  selectedStock.value = null
  stopWatchingTask()
  if (d && historyDates.value.some(h => h.date === d)) {
    await loadSavedResult(d)
  }
}

async function runSelect() {
  loading.value = true
  result.value = null
  resultSource.value = null
  fallbackNotice.value = ''
  progress.value = 0
  progressMsg.value = '启动中...'
  stopWatchingTask()
  try {
    const { data } = await startSelectStocks(date.value || undefined)
    taskId = data.task_id
    if (data.fallback && data.date !== data.requested_date) {
      fallbackNotice.value = `${data.requested_date} 暂无行情数据，已使用最近交易日 ${data.date}`
      date.value = data.date
    } else if (!date.value) {
      date.value = data.date
    }
    taskStore.trackTask(data.task_id, `选股 ${data.date}`)
    startWatchingTask(data.task_id)
  } catch {
    message.error('启动选股失败')
    loading.value = false
  }
}

onUnmounted(stopWatchingTask)

async function onRowClick(row: any) {
  selectedStock.value = row
  factorLoading.value = true
  try {
    const { data } = await getFactorDetail(date.value, row.ts_code)
    factorDetail.value = data
  } catch {
    factorDetail.value = null
  } finally {
    factorLoading.value = false
  }
}

function handleDateUpdate(ts: number | null) {
  onDateChange(ts ? formatDate(ts) : '')
}

function handleHistorySelect(d: string) {
  onDateChange(d)
}

// Factor categories for display
const factorGroups: Record<string, string[]> = {
  '价值': ['EP', 'BP'],
  '质量': ['ROE_TTM', 'GROSS_MARGIN', 'PROFIT_STB', 'MARGIN_TREND'],
  '成长': ['NET_PROFIT_YOY', 'REVENUE_YOY'],
  '动量': ['MOM_1M', 'MOM_3M', 'MOM_12M', 'REV_5D', 'IND_MOM', 'RESIDUAL_MOM', 'CMDTY_MOM'],
  '技术': ['TURN_20D', 'VOL_20D', 'PRICE_DEV_60D', 'SIZE', 'VOL_PRICE_DIV'],
  '宏观': ['MACRO_CYCLE', 'MACRO_LIQD', 'MACRO_INFL', 'MACRO_EXTR'],
  '舆情': ['POLICY_SENT', 'POLICY_INTENSITY', 'ANALYST_RATING', 'ANALYST_COVERAGE'],
}

// Factor meta: Chinese name + calculation description
const factorMeta: Record<string, { name: string; desc: string }> = {
  EP:             { name: '盈利收益率', desc: '1 / PE_TTM，值越大估值越低（正向）' },
  BP:             { name: '账面市值比', desc: '1 / PB，值越大净资产相对股价越高（正向）' },
  ROE_TTM:        { name: 'TTM净资产收益率', desc: '过去12个月滚动净利润 / 平均净资产，衡量盈利能力（正向）' },
  GROSS_MARGIN:   { name: '毛利率', desc: '最近季报毛利率（%），越高代表产品竞争力越强（正向）' },
  PROFIT_STB:     { name: '利润稳定性', desc: '近8季度净利润增速的标准差取负，波动越小得分越高（正向）' },
  MARGIN_TREND:   { name: '毛利率趋势', desc: '近4季度毛利率的线性回归斜率，斜率为正代表毛利率改善（正向）' },
  NET_PROFIT_YOY: { name: '净利润同比增速', desc: 'TTM净利润同比增长率（%），衡量盈利成长（正向）' },
  REVENUE_YOY:    { name: '营收同比增速', desc: 'TTM营业收入同比增长率（%），衡量业务扩张（正向）' },
  MOM_1M:         { name: '1个月动量', desc: '过去20个交易日累计收益率，捕捉短期趋势（正向）' },
  MOM_3M:         { name: '3个月动量', desc: '过去60个交易日累计收益率，捕捉中期趋势（正向）' },
  MOM_12M:        { name: '12个月动量', desc: '过去250日累计收益率（跳过最近20日），捕捉中长期动量（正向）' },
  REV_5D:         { name: '5日短期反转', desc: '过去5个交易日累计收益率取负，捕捉超跌反弹机会（正向）' },
  IND_MOM:        { name: '行业动量', desc: '申万一级行业近20日平均涨跌幅，行业轮动信号（正向）' },
  RESIDUAL_MOM:   { name: '残差动量', desc: '剔除市场Beta后的个股超额动量，更纯粹的选股动量（正向）' },
  TURN_20D:       { name: '20日换手率', desc: '过去20日平均换手率（%），过高换手代表短期投机风险（反向）' },
  VOL_20D:        { name: '20日波动率', desc: '过去20日收益率标准差，波动率越低风险越小（反向）' },
  PRICE_DEV_60D:  { name: '60日价格偏离度', desc: '当前价格相对60日均线的偏离程度，正偏过大有回归风险（反向）' },
  SIZE:           { name: '市值（对数）', desc: '总市值的自然对数，偏好小市值股票的风险溢价（反向）' },
  VOL_PRICE_DIV:  { name: '量价背离', desc: '成交量趋势与价格趋势的背离度，量价背离可能预示反转（反向）' },
  CMDTY_MOM:      { name: '商品动量', desc: '上游大宗商品（铜、原油、铁矿等）近月涨跌幅映射到关联行业，捕捉产业链轮动（正向）' },
  MACRO_CYCLE:    { name: '宏观景气', desc: '基于PMI、工业增加值等指标合成的经济景气度，景气上行利好周期股（正向）' },
  MACRO_LIQD:     { name: '流动性', desc: '基于SHIBOR、社融增速等指标合成的市场流动性，宽松环境利好高估值成长股（正向）' },
  MACRO_INFL:     { name: '通胀压力', desc: '基于CPI/PPI同比合成的通胀指标，通胀上行利好上游资源、消费板块（正向）' },
  MACRO_EXTR:     { name: '外部风险', desc: '基于人民币汇率、出口增速合成的外部风险指标，风险上升利好内需板块（正向）' },
  POLICY_SENT:    { name: '政策情感得分', desc: '对11个政府网站及Twitter政策账号的新闻进行NLP情感分析，正向舆情映射到相关行业个股（正向）' },
  POLICY_INTENSITY: { name: '政策强度', desc: '政策新闻的关键词强度加权得分，LLM对高强度文章做二次增强打分，反映政策力度（正向）' },
  ANALYST_RATING:   { name: '分析师评级', desc: '券商研报的共识评级得分（买入>增持>中性>减持>卖出），近90日加权平均（正向）' },
  ANALYST_COVERAGE: { name: '分析师覆盖度', desc: '近90日内覆盖该股票的研究机构数量，覆盖度越高关注度越大（正向）' },
}

onMounted(async () => {
  // Load history and latest trade date in parallel
  const [, statusRes] = await Promise.allSettled([loadHistory(), getDataStatus()])
  const latestTradeDate = statusRes.status === 'fulfilled'
    ? statusRes.value?.data?.latest_trade_date
    : null

  // Default to latest trade date with data, fall back to today
  date.value = latestTradeDate || todayStr()

  // Auto-load saved result if available, otherwise auto-run
  if (historyDates.value.some(h => h.date === date.value)) {
    await loadSavedResult(date.value)
  } else {
    runSelect()
  }
})
</script>

<template>
  <div>
    <h2 :style="{ margin: '0 0 16px 0', fontSize: '20px', fontWeight: 600, color: colors.textPrimary }">今日选股</h2>
    <n-card hoverable style="margin-bottom: 20px">
      <n-space align="center" wrap>
        <n-button
          :type="result ? 'warning' : 'primary'"
          :loading="loading"
          :disabled="!date"
          @click="runSelect"
        >
          {{ result ? '重新执行' : '执行今日选股' }}
        </n-button>
        <n-tag v-if="resultSource === 'saved'" type="info" size="small">已存结果</n-tag>
        <n-tag v-else-if="resultSource === 'live'" type="success" size="small">实时计算</n-tag>
        <n-divider vertical />
        <n-date-picker
          type="date"
          :value="date ? new Date(date).getTime() : null"
          @update:value="handleDateUpdate"
          clearable
          placeholder="选择日期（默认今日）"
        />
        <n-select
          v-if="historyOptions.length"
          :value="selectedDateInHistory ? date : null"
          :options="historyOptions"
          placeholder="历史选股结果"
          style="min-width: 200px"
          clearable
          @update:value="handleHistorySelect"
        />
      </n-space>
      <div v-if="result" :style="{ marginTop: '8px', color: colors.textTertiary, fontSize: '13px' }">
        {{ result.date }} · 共 {{ result.total }} 只股票参与打分
      </div>
      <n-alert v-if="fallbackNotice" type="warning" :show-icon="true" style="margin-top: 8px" closable @close="fallbackNotice = ''">
        {{ fallbackNotice }}
      </n-alert>
    </n-card>

    <!-- Loading progress -->
    <n-card hoverable style="margin-bottom: 20px" v-if="loading">
      <div style="text-align: center; padding: 40px 0">
        <n-spin size="large" />
        <div :style="{ marginTop: '12px', color: colors.textTertiary }">{{ progressMsg }}</div>
        <n-progress
          type="line"
          :percentage="progress"
          :height="12"
          style="max-width: 400px; margin: 16px auto 0"
        />
      </div>
    </n-card>

    <n-grid :cols="24" :x-gap="20" v-if="result">
      <n-gi :span="16">
        <n-card hoverable title="Top 选股结果">
          <StockTable
            :stocks="result?.top_stocks || []"
            @row-click="onRowClick"
          />
        </n-card>

        <!-- By industry -->
        <n-card hoverable style="margin-top: 20px" v-if="result?.by_industry" title="分行业选股">
          <n-collapse>
            <n-collapse-item
              v-for="(stocks, industry) in result.by_industry"
              :key="industry"
              :title="`${industry} (${stocks.length}只)`"
              :name="industry"
            >
              <StockTable :stocks="stocks" @row-click="onRowClick" />
            </n-collapse-item>
          </n-collapse>
        </n-card>
      </n-gi>

      <n-gi :span="8">
        <n-spin :show="factorLoading">
          <n-card hoverable>
            <template #header>
              <span>因子明细 {{ selectedStock ? `- ${selectedStock.ts_code}` : '' }}</span>
            </template>
            <div v-if="factorDetail">
              <!-- Composite score -->
              <div :style="{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '6px 0 10px', marginBottom: '10px', borderBottom: `1px solid ${colors.borderLight}`, fontSize: '13px' }">
                <span :style="{ fontWeight: 600, color: colors.textPrimary }">综合得分</span>
                <span style="font-size: 15px; font-weight: 700" :style="{ color: pnlColor(factorDetail.score) }">
                  {{ factorDetail.score != null ? Number(factorDetail.score).toFixed(4) : '-' }}
                </span>
              </div>
              <!-- Factor groups -->
              <div v-for="(factors, group) in factorGroups" :key="group" style="margin-bottom: 16px">
                <div :style="{ fontWeight: 600, marginBottom: '8px', color: colors.textPrimary }">{{ group }}</div>
                <div v-for="f in factors" :key="f" style="display: flex; justify-content: space-between; align-items: center; padding: 2px 0; font-size: 13px">
                  <span :style="{ color: colors.textSecondary, display: 'flex', alignItems: 'center', gap: '3px' }">
                    {{ f }}
                    <n-tooltip v-if="factorMeta[f]" trigger="hover" placement="right" :style="{ maxWidth: '240px' }">
                      <template #trigger>
                        <span :style="{ cursor: 'help', color: colors.textDisabled, fontSize: '11px', lineHeight: 1, userSelect: 'none' }">ⓘ</span>
                      </template>
                      <div>
                        <div style="font-weight: 600; margin-bottom: 4px">{{ factorMeta[f].name }}</div>
                        <div style="font-size: 12px; opacity: 0.85">{{ factorMeta[f].desc }}</div>
                      </div>
                    </n-tooltip>
                  </span>
                  <span :style="{ color: pnlColor(factorDetail[f]) }">
                    {{ factorDetail[f] != null ? factorDetail[f].toFixed(3) : '-' }}
                  </span>
                </div>
              </div>
            </div>
            <n-empty v-else description="点击左侧表格行查看因子" />
          </n-card>
        </n-spin>
      </n-gi>
    </n-grid>

    <n-empty v-if="!loading && !result" description="选择日期后点击「执行选股」开始" />
  </div>
</template>
