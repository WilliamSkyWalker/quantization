/**
 * PaperTrading.vue tests — account, positions, NAV, trades.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushAll, mountView } from './helpers'
import PaperTrading from '../views/PaperTrading.vue'
import {
  paperAccountResponse,
  paperPositionsResponse,
  paperNavResponse,
  paperTransactionsResponse,
} from './fixtures'

vi.mock('../api')
import {
  getPaperAccount,
  getPaperPositions,
  getPaperNav,
  getPaperTransactions,
  startPaperTrade,
  resetPaper,
} from '../api'

describe('PaperTrading.vue', () => {
  beforeEach(() => {
    vi.mocked(getPaperAccount).mockResolvedValue({ data: paperAccountResponse } as any)
    vi.mocked(getPaperPositions).mockResolvedValue({ data: paperPositionsResponse } as any)
    vi.mocked(getPaperNav).mockResolvedValue({ data: paperNavResponse } as any)
    vi.mocked(getPaperTransactions).mockResolvedValue({ data: paperTransactionsResponse } as any)
    vi.mocked(startPaperTrade).mockResolvedValue({ data: { task_id: 'pt1', name: '执行交易信号' } } as any)
    vi.mocked(resetPaper).mockResolvedValue({ data: {} } as any)
  })

  it('calls all 4 paper APIs on mount', async () => {
    mountView(PaperTrading)
    await flushAll()
    expect(getPaperAccount).toHaveBeenCalled()
    expect(getPaperPositions).toHaveBeenCalled()
    expect(getPaperNav).toHaveBeenCalled()
    expect(getPaperTransactions).toHaveBeenCalled()
  })

  it('renders account metrics', async () => {
    const w = mountView(PaperTrading)
    await flushAll()
    expect(w.text()).toContain('初始资金')
    expect(w.text()).toContain('1,000,000')
    expect(w.text()).toContain('总资产')
    expect(w.text()).toContain('盈亏')
  })

  it('renders position table with stocks', async () => {
    const w = mountView(PaperTrading)
    await flushAll()
    expect(w.text()).toContain('当前持仓')
    expect(w.text()).toContain('招商银行')
    expect(w.text()).toContain('五粮液')
  })

  it('renders trade log', async () => {
    const w = mountView(PaperTrading)
    await flushAll()
    expect(w.text()).toContain('最近交易')
    expect(w.text()).toContain('招商银行')
    expect(w.text()).toContain('平安银行')
  })

  it('has action buttons', async () => {
    const w = mountView(PaperTrading)
    await flushAll()
    expect(w.text()).toContain('执行今日交易')
    expect(w.text()).toContain('重置账户')
  })

  it('renders NAV chart section', async () => {
    const w = mountView(PaperTrading)
    await flushAll()
    expect(w.text()).toContain('净值曲线')
  })
})
