/**
 * Settings.vue tests — loads settings and industry factors, renders form.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushAll, mountView } from './helpers'
import Settings from '../views/Settings.vue'
import { settingsResponse, industryFactorsResponse } from './fixtures'

vi.mock('../api')
import { getSettings, updateSettings, getIndustryFactors } from '../api'

describe('Settings.vue', () => {
  beforeEach(() => {
    vi.mocked(getSettings).mockResolvedValue({ data: { ...settingsResponse } } as any)
    vi.mocked(updateSettings).mockResolvedValue({ data: { message: 'ok' } } as any)
    vi.mocked(getIndustryFactors).mockResolvedValue({ data: industryFactorsResponse } as any)
  })

  it('calls settings and industry factors APIs on mount', async () => {
    mountView(Settings)
    await flushAll()
    expect(getSettings).toHaveBeenCalled()
    expect(getIndustryFactors).toHaveBeenCalled()
  })

  it('renders sensitive config section', async () => {
    const w = mountView(Settings)
    await flushAll()
    expect(w.text()).toContain('凭证配置')
    expect(w.text()).toContain('TUSHARE_TOKEN')
    expect(w.text()).toContain('***已配置***')
  })

  it('renders setting groups', async () => {
    const w = mountView(Settings)
    await flushAll()
    expect(w.text()).toContain('策略参数')
    expect(w.text()).toContain('交易成本')
    expect(w.text()).toContain('风控参数')
    expect(w.text()).toContain('波动率目标')
    expect(w.text()).toContain('模拟交易')
    expect(w.text()).toContain('系统')
  })

  it('renders setting keys', async () => {
    const w = mountView(Settings)
    await flushAll()
    expect(w.text()).toContain('MAX_HOLDINGS')
    expect(w.text()).toContain('BUY_COMMISSION')
    expect(w.text()).toContain('MAX_DRAWDOWN_THRESHOLD')
    expect(w.text()).toContain('NEUTRALIZE_MODE')
  })

  it('has save button', async () => {
    const w = mountView(Settings)
    await flushAll()
    expect(w.text()).toContain('保存配置')
  })

  it('clicking save calls updateSettings', async () => {
    const w = mountView(Settings)
    await flushAll()
    const btn = w.findAll('button').find(b => b.text().includes('保存配置'))
    await btn!.trigger('click')
    await flushAll()
    expect(updateSettings).toHaveBeenCalled()
  })

  it('renders industry factor weights section', async () => {
    const w = mountView(Settings)
    await flushAll()
    expect(w.text()).toContain('行业因子权重')
    expect(w.text()).toContain('银行')
    expect(w.text()).toContain('电子')
  })
})
