/**
 * Dashboard.vue tests — stat cards load from 4 APIs, NAV chart renders.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushAll, mountView } from './helpers'
import Dashboard from '../views/Dashboard.vue'
import {
  dataStatusResponse,
  paperAccountResponse,
  paperNavResponse,
  sentimentStatusResponse,
} from './fixtures'

vi.mock('../api')
import { getDataStatus, getPaperAccount, getPaperNav, getSentimentStatus } from '../api'

describe('Dashboard.vue', () => {
  beforeEach(() => {
    vi.mocked(getDataStatus).mockResolvedValue({ data: dataStatusResponse } as any)
    vi.mocked(getPaperAccount).mockResolvedValue({ data: paperAccountResponse } as any)
    vi.mocked(getPaperNav).mockResolvedValue({ data: paperNavResponse } as any)
    vi.mocked(getSentimentStatus).mockResolvedValue({ data: sentimentStatusResponse } as any)
  })

  it('calls all 4 APIs on mount', async () => {
    mountView(Dashboard)
    await flushAll()
    expect(getDataStatus).toHaveBeenCalled()
    expect(getPaperAccount).toHaveBeenCalled()
    expect(getPaperNav).toHaveBeenCalled()
    expect(getSentimentStatus).toHaveBeenCalled()
  })

  it('renders stock count from data status', async () => {
    const w = mountView(Dashboard)
    await flushAll()
    expect(w.text()).toContain('5,342')
    expect(w.text()).toContain('股票数量')
  })

  it('renders latest trade date', async () => {
    const w = mountView(Dashboard)
    await flushAll()
    expect(w.text()).toContain('2025-02-26')
  })

  it('renders paper nav value', async () => {
    const w = mountView(Dashboard)
    await flushAll()
    // 1125000 / 1000000 = 1.125
    expect(w.text()).toContain('1.1250')
  })

  it('renders article count', async () => {
    const w = mountView(Dashboard)
    await flushAll()
    expect(w.text()).toContain('690')
    expect(w.text()).toContain('舆情文章数')
  })

  it('renders NavChart section when nav data exists', async () => {
    const w = mountView(Dashboard)
    await flushAll()
    // NavChart component is rendered (stubbed by mountView as echarts-stub)
    expect(w.text()).toContain('模拟盘净值曲线')
  })

  it('handles API failure gracefully (Promise.allSettled)', async () => {
    vi.mocked(getDataStatus).mockRejectedValueOnce(new Error('network'))
    const w = mountView(Dashboard)
    await flushAll()
    // Should not throw — other cards still render
    expect(w.text()).toContain('690') // sentiment still works
  })
})
