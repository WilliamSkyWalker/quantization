<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useMessage, NTag, NIcon } from 'naive-ui'
import { CheckmarkOutline, AddOutline } from '@vicons/ionicons5'
import { getSettings, updateSettings, getIndustryFactors, getAllIndustries } from '../api'
import { colors } from '../theme'
import { useResponsive } from '../composables/useResponsive'

const { isMobile } = useResponsive()

const message = useMessage()
const loading = ref(false)
const settings = ref<Record<string, any>>({})
const sensitive = ref<Record<string, string>>({})
const industryFactors = ref<Record<string, any>>({})

// Industry whitelist
const allIndustries = ref<string[]>([])
const allowedIndustries = ref<string[]>([])
const industryToAdd = ref<string | null>(null)

const availableIndustries = computed(() =>
  allIndustries.value
    .filter(i => !allowedIndustries.value.includes(i))
    .map(i => ({ label: i, value: i }))
)

const settingGroups = [
  {
    label: '策略参数',
    keys: ['MAX_HOLDINGS', 'MIN_HOLDINGS', 'MIN_SELECT_SCORE', 'MAX_SINGLE_WEIGHT', 'MAX_INDUSTRY_WEIGHT'],
  },
  {
    label: '交易成本',
    keys: ['BUY_COMMISSION', 'SELL_COMMISSION', 'STAMP_TAX', 'SLIPPAGE'],
  },
  {
    label: '风控参数',
    keys: ['MAX_DRAWDOWN_THRESHOLD', 'DRAWDOWN_REDUCE_POSITION', 'MIN_DAILY_TURNOVER', 'IPO_FILTER_DAYS'],
  },
  {
    label: '中性化',
    keys: ['NEUTRALIZE_MODE', 'NONLINEAR_SIZE'],
  },
  {
    label: '波动率目标',
    keys: ['USE_VOL_TARGETING', 'TARGET_VOL', 'VOL_LOOKBACK_DAYS', 'VOL_SCALE_MIN', 'VOL_SCALE_MAX'],
  },
  {
    label: '模拟交易',
    keys: ['PAPER_INITIAL_CAPITAL', 'PAPER_ACCOUNT_NAME', 'TRADER_TYPE'],
  },
  {
    label: '系统',
    keys: ['DATA_START_DATE', 'EXCLUDE_STAR_MARKET', 'LOG_LEVEL'],
  },
]

async function loadSettings() {
  loading.value = true
  try {
    const [settingsRes, factorsRes, industriesRes] = await Promise.allSettled([
      getSettings(),
      getIndustryFactors(),
      getAllIndustries(),
    ])
    if (settingsRes.status === 'fulfilled') {
      const data = settingsRes.value.data
      sensitive.value = data._sensitive || {}
      delete data._sensitive
      // Extract ALLOWED_INDUSTRIES separately
      const ai = data.ALLOWED_INDUSTRIES
      allowedIndustries.value = Array.isArray(ai) ? ai : []
      delete data.ALLOWED_INDUSTRIES
      settings.value = data
    }
    if (factorsRes.status === 'fulfilled') {
      industryFactors.value = factorsRes.value.data.industries
    }
    if (industriesRes.status === 'fulfilled') {
      allIndustries.value = industriesRes.value.data.industries || []
    }
  } finally {
    loading.value = false
  }
}

function addIndustry() {
  if (industryToAdd.value && !allowedIndustries.value.includes(industryToAdd.value)) {
    allowedIndustries.value.push(industryToAdd.value)
  }
  industryToAdd.value = null
}

function removeIndustry(name: string) {
  allowedIndustries.value = allowedIndustries.value.filter(i => i !== name)
}

async function saveSettings() {
  try {
    await updateSettings({
      ...settings.value,
      ALLOWED_INDUSTRIES: allowedIndustries.value,
    })
    message.success('配置已保存，重启后端生效')
  } catch (e: any) {
    message.error('保存失败')
  }
}

onMounted(loadSettings)
</script>

<template>
  <n-spin :show="loading">
    <!-- Sensitive settings -->
    <n-card hoverable style="margin-bottom: 20px" title="凭证配置 (显示状态)">
      <n-descriptions :column="isMobile ? 1 : 3" bordered label-placement="left" size="small">
        <n-descriptions-item v-for="(val, key) in sensitive" :key="key" :label="String(key)">
          <n-tag :type="val ? 'success' : 'default'" size="small">{{ val || '未配置' }}</n-tag>
        </n-descriptions-item>
      </n-descriptions>
    </n-card>

    <!-- Editable settings -->
    <n-card hoverable style="margin-bottom: 20px" v-for="group in settingGroups" :key="group.label" :title="group.label">
      <n-form :label-width="isMobile ? 120 : 200" size="small" :label-placement="isMobile ? 'top' : 'left'">
        <n-form-item v-for="key in group.keys" :key="key" :label="key">
          <n-input
            v-if="typeof settings[key] === 'string'"
            v-model:value="settings[key]"
            style="max-width: 300px"
          />
          <n-input-number
            v-else-if="typeof settings[key] === 'number'"
            v-model:value="settings[key]"
            :step="settings[key] < 1 ? 0.001 : 1"
            style="max-width: 300px"
          />
          <n-switch
            v-else-if="typeof settings[key] === 'boolean'"
            v-model:value="settings[key]"
          />
          <n-input v-else v-model:value="settings[key]" style="max-width: 300px" />
        </n-form-item>
      </n-form>
    </n-card>

    <!-- Industry whitelist -->
    <n-card hoverable style="margin-bottom: 20px" title="行业白名单">
      <template #header-extra>
        <span :style="{ fontSize: '12px', color: colors.textTertiary }">
          {{ allowedIndustries.length === 0 ? '不限制（允许所有行业）' : `已选 ${allowedIndustries.length} 个行业` }}
        </span>
      </template>

      <div style="margin-bottom: 12px">
        <n-space>
          <n-select
            v-model:value="industryToAdd"
            :options="availableIndustries"
            placeholder="选择行业"
            filterable
            clearable
            style="width: 200px"
          />
          <n-button type="primary" size="small" :disabled="!industryToAdd" @click="addIndustry">
            <template #icon><n-icon><AddOutline /></n-icon></template>
            添加
          </n-button>
        </n-space>
      </div>

      <n-space v-if="allowedIndustries.length > 0">
        <n-tag
          v-for="name in allowedIndustries"
          :key="name"
          closable
          type="info"
          @close="removeIndustry(name)"
        >
          {{ name }}
        </n-tag>
      </n-space>
      <div v-else :style="{ color: colors.textTertiary, fontSize: '13px' }">
        空白名单表示允许所有行业，添加行业后将仅允许买入已选行业的股票
      </div>
    </n-card>

    <div style="text-align: right; margin-bottom: 20px">
      <n-button type="primary" @click="saveSettings" size="large">
        <template #icon><n-icon><CheckmarkOutline /></n-icon></template>
        保存配置
      </n-button>
    </div>

    <!-- Industry factor weights -->
    <n-card hoverable v-if="Object.keys(industryFactors).length > 0" title="行业因子权重">
      <n-collapse>
        <n-collapse-item v-for="(factors, industry) in industryFactors" :key="industry" :title="String(industry)" :name="String(industry)">
          <div v-for="(info, factor) in factors" :key="factor" style="display: flex; align-items: center; gap: 12px; margin-bottom: 8px">
            <span style="width: 200px; font-size: 13px">{{ factor }}</span>
            <n-input-number v-model:value="info.weight" :step="0.1" size="small" />
            <span :style="{ fontSize: '12px', color: colors.textTertiary }">{{ info.description }}</span>
          </div>
        </n-collapse-item>
      </n-collapse>
    </n-card>
  </n-spin>
</template>
