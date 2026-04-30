<script setup lang="ts">
import { ref, computed, h } from 'vue'
import { useMessage } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { useTaskPolling } from '../composables/useTaskPolling'
import { colors, semanticColor } from '../theme'
import { runUsStockPnl, runUsStockPnlFromDb, runBacktest } from '../api/polymarket'

const message = useMessage()

// ---------- 数据来源 ----------
const dataSource = ref<'alerts' | 'db'>('db')
const alertsJson = ref<any[]>([])

// Polymarket 回测 (内嵌)
const {
  loading: pmLoading,
  result: pmResult,
  start: pmStart,
} = useTaskPolling({ taskLabel: 'Polymarket 回测' })

async function runPolymarketBacktest() {
  try {
    const { data } = await runBacktest({ use_llm: true })
    pmStart(data.task_id)
  } catch (e: any) {
    message.error('Polymarket 回测启动失败: ' + (e.response?.data?.error || e.message))
  }
}

const pmAlertCount = computed(() => {
  if (!pmResult.value) return 0
  return pmResult.value.alerts?.length || 0
})

// ---------- 配置 ----------
const holdingDays = ref(5)
const minConfidence = ref(0)
const dbLimit = ref(200)

// ---------- P&L 回测 ----------
const {
  loading: pnlLoading,
  result: pnlResult,
  start: pnlStart,
} = useTaskPolling({ taskLabel: '美股 P&L 回测' })

async function runPnlBacktest() {
  try {
    if (dataSource.value === 'alerts') {
      const alerts = pmResult.value?.alerts || alertsJson.value
      if (!alerts || alerts.length === 0) {
        message.warning('请先运行 Polymarket 回测以获取告警数据')
        return
      }
      const { data } = await runUsStockPnl({
        alerts,
        holding_days: holdingDays.value,
        min_confidence: minConfidence.value,
      })
      pnlStart(data.task_id)
    } else {
      const { data } = await runUsStockPnlFromDb({
        holding_days: holdingDays.value,
        min_confidence: minConfidence.value,
        limit: dbLimit.value,
      })
      pnlStart(data.task_id)
    }
  } catch (e: any) {
    message.error('美股 P&L 回测失败: ' + (e.response?.data?.error || e.message))
  }
}

const summary = computed(() => pnlResult.value?.summary || null)
const trades = computed(() => pnlResult.value?.trades || [])
const config = computed(() => pnlResult.value?.config || null)

// ---------- 格式化 ----------
function fmtPct(val: number | null | undefined): string {
  if (val == null) return '-'
  return (val >= 0 ? '+' : '') + val.toFixed(2) + '%'
}

function fmtRate(val: number | null | undefined): string {
  if (val == null) return '-'
  return (val * 100).toFixed(0) + '%'
}

function dirLabel(d: string): string {
  return d === 'bullish' ? '看多' : d === 'bearish' ? '看空' : d
}

function alertLabel(t: string): string {
  const m: Record<string, string> = {
    spike_5m: '5分钟',
    spike_1h: '1小时',
    spike_24h: '24小时',
  }
  return m[t] || t
}

// ---------- 表格列 ----------
const tradeColumns: DataTableColumns = [
  {
    title: '事件',
    key: 'event_question',
    width: 200,
    ellipsis: { tooltip: true },
  },
  { title: 'Ticker', key: 'ticker', width: 80 },
  {
    title: '方向',
    key: 'direction',
    width: 70,
    render: (row: any) => dirLabel(row.direction),
  },
  {
    title: '置信度',
    key: 'confidence',
    width: 70,
    render: (row: any) => row.confidence?.toFixed(2),
  },
  { title: '入场日', key: 'entry_date', width: 100 },
  {
    title: '入场价',
    key: 'entry_price',
    width: 90,
    render: (row: any) => '$' + row.entry_price?.toFixed(2),
  },
  { title: '出场日', key: 'exit_date', width: 100 },
  {
    title: '出场价',
    key: 'exit_price',
    width: 90,
    render: (row: any) => '$' + row.exit_price?.toFixed(2),
  },
  {
    title: '收益率',
    key: 'return_pct',
    width: 90,
    sorter: (a: any, b: any) => a.return_pct - b.return_pct,
    render: (row: any) =>
      h(
        'span',
        { style: { color: semanticColor(row.return_pct), fontWeight: 600 } },
        fmtPct(row.return_pct),
      ),
  },
  {
    title: '结果',
    key: 'is_win',
    width: 90,
    render: (row: any) =>
      h(
        'span',
        {
          style: {
            color: row.is_win ? colors.positive : colors.negative,
            fontWeight: 600,
          },
        },
        (row.is_win ? '盈利' : '亏损') + (row.is_mark_to_market ? ' (MTM)' : ''),
      ),
  },
  {
    title: '持仓天数',
    key: 'holding_days',
    width: 80,
    render: (row: any) => {
      const label = row.holding_days + (row.is_mark_to_market ? ' (MTM)' : '')
      return label
    },
  },
  {
    title: '告警类型',
    key: 'alert_type',
    width: 80,
    render: (row: any) => alertLabel(row.alert_type),
  },
]

const tickerColumns: DataTableColumns = [
  { title: 'Ticker', key: 'ticker', width: 80 },
  { title: '交易次数', key: 'count', width: 90, sorter: (a: any, b: any) => a.count - b.count },
  {
    title: '胜率',
    key: 'win_rate',
    width: 80,
    sorter: (a: any, b: any) => a.win_rate - b.win_rate,
    render: (row: any) => fmtRate(row.win_rate),
  },
  {
    title: '平均收益',
    key: 'avg_return',
    width: 90,
    sorter: (a: any, b: any) => a.avg_return - b.avg_return,
    render: (row: any) =>
      h(
        'span',
        { style: { color: semanticColor(row.avg_return) } },
        fmtPct(row.avg_return),
      ),
  },
]

const topColumns: DataTableColumns = [
  { title: 'Ticker', key: 'ticker', width: 80 },
  {
    title: '事件',
    key: 'event_question',
    width: 180,
    ellipsis: { tooltip: true },
  },
  {
    title: '方向',
    key: 'direction',
    width: 60,
    render: (row: any) => dirLabel(row.direction),
  },
  {
    title: '收益率',
    key: 'return_pct',
    width: 90,
    render: (row: any) =>
      h(
        'span',
        { style: { color: semanticColor(row.return_pct), fontWeight: 600 } },
        fmtPct(row.return_pct),
      ),
  },
  { title: '入场日', key: 'entry_date', width: 95 },
  { title: '出场日', key: 'exit_date', width: 95 },
]
</script>

<template>
  <n-space vertical :size="16">
    <!-- 数据来源 + 配置 -->
    <n-card title="数据来源与配置" size="small">
      <n-space vertical :size="16">
        <n-radio-group v-model:value="dataSource">
          <n-space>
            <n-radio value="alerts">从 Polymarket 回测结果</n-radio>
            <n-radio value="db">从历史告警 (DB)</n-radio>
          </n-space>
        </n-radio-group>

        <!-- 回测结果模式 -->
        <template v-if="dataSource === 'alerts'">
          <n-space align="center">
            <n-button type="info" :loading="pmLoading" @click="runPolymarketBacktest" size="small">
              运行 Polymarket 回测
            </n-button>
            <span v-if="pmAlertCount > 0" :style="{ color: colors.positive, fontWeight: 600 }">
              已有 {{ pmAlertCount }} 条告警
            </span>
            <span v-else :style="{ color: colors.neutral }">尚无告警数据</span>
          </n-space>
        </template>

        <!-- DB 模式 -->
        <template v-if="dataSource === 'db'">
          <n-space align="center">
            <span>告警数量上限:</span>
            <n-input-number v-model:value="dbLimit" :min="10" :max="1000" :step="50" size="small" style="width: 120px" />
          </n-space>
        </template>

        <n-grid :cols="3" :x-gap="16">
          <n-gi>
            <n-space align="center">
              <span>持仓天数:</span>
              <n-input-number v-model:value="holdingDays" :min="1" :max="60" size="small" style="width: 100px" />
            </n-space>
          </n-gi>
          <n-gi>
            <n-space align="center">
              <span>最低置信度:</span>
              <n-input-number v-model:value="minConfidence" :min="0" :max="1" :step="0.1" size="small" style="width: 100px" />
            </n-space>
          </n-gi>
          <n-gi>
            <n-button type="primary" :loading="pnlLoading" @click="runPnlBacktest" :disabled="pnlLoading">
              运行美股 P&L 回测
            </n-button>
          </n-gi>
        </n-grid>
      </n-space>
    </n-card>

    <!-- 汇总统计 -->
    <template v-if="summary && summary.total_trades > 0">
      <n-card title="汇总统计" size="small">
        <n-grid :cols="4" :x-gap="16" :y-gap="12">
          <n-gi>
            <n-statistic label="总交易数">
              <template #default>{{ summary.total_trades }}</template>
              <template #suffix v-if="summary.mtm_trades > 0">
                <span style="font-size: 13px; color: #999; margin-left: 4px">
                  ({{ summary.settled_trades }}已结 / {{ summary.mtm_trades }}持仓中)
                </span>
              </template>
            </n-statistic>
          </n-gi>
          <n-gi>
            <n-statistic label="胜率">
              <template #default>
                <span :style="{ color: summary.win_rate >= 0.5 ? colors.positive : colors.negative }">
                  {{ fmtRate(summary.win_rate) }}
                </span>
              </template>
              <template #suffix>
                <span style="font-size: 13px; color: #999; margin-left: 4px">
                  ({{ summary.win_count }}W / {{ summary.loss_count }}L)
                </span>
              </template>
            </n-statistic>
          </n-gi>
          <n-gi>
            <n-statistic label="平均收益">
              <template #default>
                <span :style="{ color: summary.avg_return_pct >= 0 ? colors.positive : colors.negative }">
                  {{ fmtPct(summary.avg_return_pct) }}
                </span>
              </template>
            </n-statistic>
          </n-gi>
          <n-gi>
            <n-statistic label="夏普比率">
              <template #default>
                <span :style="{ color: summary.sharpe_ratio >= 0 ? colors.positive : colors.negative }">
                  {{ summary.sharpe_ratio?.toFixed(2) || '-' }}
                </span>
              </template>
            </n-statistic>
          </n-gi>

          <n-gi>
            <n-statistic label="总收益" tabular-nums>
              <template #default>
                <span :style="{ color: summary.total_return_pct >= 0 ? colors.positive : colors.negative }">
                  {{ fmtPct(summary.total_return_pct) }}
                </span>
              </template>
            </n-statistic>
          </n-gi>
          <n-gi>
            <n-statistic label="中位收益">
              <template #default>
                <span :style="{ color: summary.median_return_pct >= 0 ? colors.positive : colors.negative }">
                  {{ fmtPct(summary.median_return_pct) }}
                </span>
              </template>
            </n-statistic>
          </n-gi>
          <n-gi>
            <n-statistic label="最大单笔盈利">
              <template #default>
                <span :style="{ color: colors.positive }">{{ fmtPct(summary.max_single_win_pct) }}</span>
              </template>
            </n-statistic>
          </n-gi>
          <n-gi>
            <n-statistic label="最大单笔亏损">
              <template #default>
                <span :style="{ color: colors.negative }">{{ fmtPct(summary.max_single_loss_pct) }}</span>
              </template>
            </n-statistic>
          </n-gi>

          <n-gi>
            <n-statistic label="平均盈利">
              <template #default>
                <span :style="{ color: colors.positive }">{{ fmtPct(summary.avg_win_return_pct) }}</span>
              </template>
            </n-statistic>
          </n-gi>
          <n-gi>
            <n-statistic label="平均亏损">
              <template #default>
                <span :style="{ color: colors.negative }">{{ fmtPct(summary.avg_loss_return_pct) }}</span>
              </template>
            </n-statistic>
          </n-gi>
          <n-gi>
            <n-statistic label="盈亏比">
              <template #default>
                {{ summary.profit_factor != null ? summary.profit_factor.toFixed(2) : '-' }}
              </template>
            </n-statistic>
          </n-gi>
          <n-gi>
            <n-statistic label="持仓天数">
              <template #default>{{ config?.holding_days || '-' }}</template>
            </n-statistic>
          </n-gi>
        </n-grid>
      </n-card>

      <!-- 分组统计 -->
      <n-grid :cols="3" :x-gap="16">
        <!-- 按方向 -->
        <n-gi>
          <n-card title="按方向" size="small">
            <n-space vertical :size="8">
              <div v-for="(val, key) in summary.by_direction" :key="key" style="display: flex; justify-content: space-between; align-items: center; padding: 6px 0; border-bottom: 1px solid #f0f2f5">
                <span style="font-weight: 600">{{ dirLabel(key as string) }}</span>
                <n-space :size="12">
                  <span>{{ val.count }} 笔</span>
                  <span :style="{ color: val.win_rate >= 0.5 ? colors.positive : colors.negative }">
                    {{ fmtRate(val.win_rate) }}
                  </span>
                  <span :style="{ color: val.avg_return >= 0 ? colors.positive : colors.negative }">
                    {{ fmtPct(val.avg_return) }}
                  </span>
                </n-space>
              </div>
              <div v-if="!summary.by_direction || Object.keys(summary.by_direction).length === 0" style="color: #999; text-align: center; padding: 12px 0">
                暂无数据
              </div>
            </n-space>
          </n-card>
        </n-gi>

        <!-- 按告警类型 -->
        <n-gi>
          <n-card title="按告警类型" size="small">
            <n-space vertical :size="8">
              <div v-for="(val, key) in summary.by_alert_type" :key="key" style="display: flex; justify-content: space-between; align-items: center; padding: 6px 0; border-bottom: 1px solid #f0f2f5">
                <span style="font-weight: 600">{{ alertLabel(key as string) }}</span>
                <n-space :size="12">
                  <span>{{ val.count }} 笔</span>
                  <span :style="{ color: val.win_rate >= 0.5 ? colors.positive : colors.negative }">
                    {{ fmtRate(val.win_rate) }}
                  </span>
                  <span :style="{ color: val.avg_return >= 0 ? colors.positive : colors.negative }">
                    {{ fmtPct(val.avg_return) }}
                  </span>
                </n-space>
              </div>
              <div v-if="!summary.by_alert_type || Object.keys(summary.by_alert_type).length === 0" style="color: #999; text-align: center; padding: 12px 0">
                暂无数据
              </div>
            </n-space>
          </n-card>
        </n-gi>

        <!-- 按置信度 -->
        <n-gi>
          <n-card title="按置信度" size="small">
            <n-space vertical :size="8">
              <div v-for="(val, key) in summary.by_confidence_tier" :key="key" style="display: flex; justify-content: space-between; align-items: center; padding: 6px 0; border-bottom: 1px solid #f0f2f5">
                <span style="font-weight: 600">{{ key }}</span>
                <n-space :size="12">
                  <span>{{ val.count }} 笔</span>
                  <span :style="{ color: val.win_rate >= 0.5 ? colors.positive : colors.negative }">
                    {{ fmtRate(val.win_rate) }}
                  </span>
                  <span :style="{ color: val.avg_return >= 0 ? colors.positive : colors.negative }">
                    {{ fmtPct(val.avg_return) }}
                  </span>
                </n-space>
              </div>
              <div v-if="!summary.by_confidence_tier || Object.keys(summary.by_confidence_tier).length === 0" style="color: #999; text-align: center; padding: 12px 0">
                暂无数据
              </div>
            </n-space>
          </n-card>
        </n-gi>
      </n-grid>

      <!-- Top Ticker -->
      <n-card title="Top Ticker 表现" size="small" v-if="summary.ticker_stats?.length">
        <n-data-table
          :columns="tickerColumns"
          :data="summary.ticker_stats"
          :max-height="350"
          size="small"
          striped
          :row-key="(row: any) => row.ticker"
        />
      </n-card>

      <!-- Top 盈利 / 亏损 -->
      <n-grid :cols="2" :x-gap="16" v-if="summary.top_winners?.length || summary.top_losers?.length">
        <n-gi>
          <n-card title="Top 10 盈利交易" size="small">
            <n-data-table
              :columns="topColumns"
              :data="summary.top_winners || []"
              :max-height="350"
              size="small"
              striped
              :row-key="(row: any) => row.ticker + row.entry_date"
            />
          </n-card>
        </n-gi>
        <n-gi>
          <n-card title="Top 10 亏损交易" size="small">
            <n-data-table
              :columns="topColumns"
              :data="summary.top_losers || []"
              :max-height="350"
              size="small"
              striped
              :row-key="(row: any) => row.ticker + row.entry_date"
            />
          </n-card>
        </n-gi>
      </n-grid>

      <!-- 全部交易明细 -->
      <n-card title="全部交易明细" size="small">
        <n-data-table
          :columns="tradeColumns"
          :data="trades"
          :max-height="500"
          :scroll-x="1100"
          size="small"
          striped
          :pagination="{ pageSize: 20 }"
          :row-key="(row: any) => row.alert_idx + '-' + row.ticker + '-' + row.entry_date"
        />
      </n-card>
    </template>

    <!-- 无结果提示 -->
    <n-card v-else-if="pnlResult && (!summary || summary.total_trades === 0)" size="small">
      <n-empty description="无交易结果。可能告警中没有含有美股 ticker 的信号，或股价数据缺失。" />
    </n-card>
  </n-space>
</template>
