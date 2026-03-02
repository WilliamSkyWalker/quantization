/**
 * StockSelect.vue tests — starts async selection task, polls for results.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mountView } from './helpers'
import StockSelect from '../views/StockSelect.vue'
import { selectResponse, factorDetailResponse } from './fixtures'

vi.mock('../api')
import { startSelectStocks, getSelectResult, getFactorDetail } from '../api'

async function flush() {
  for (let i = 0; i < 5; i++) {
    await Promise.resolve()
  }
}

describe('StockSelect.vue', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.mocked(startSelectStocks).mockResolvedValue({
      data: { task_id: 'sel-001', date: '2025-02-26' },
    } as any)
    vi.mocked(getSelectResult).mockResolvedValue({
      data: { status: 'completed', progress: 100, result: selectResponse },
    } as any)
    vi.mocked(getFactorDetail).mockResolvedValue({ data: factorDetailResponse } as any)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  async function mountAndWait() {
    const w = mountView(StockSelect)
    // flush the initial startSelectStocks promise
    await flush()
    // advance timer to trigger the polling interval (1500ms)
    await vi.advanceTimersByTimeAsync(2000)
    // flush the getSelectResult promise
    await flush()
    return w
  }

  it('calls startSelectStocks on mount', async () => {
    await mountAndWait()
    expect(startSelectStocks).toHaveBeenCalled()
  })

  it('renders total stock count after polling completes', async () => {
    const w = await mountAndWait()
    expect(w.text()).toContain('4200')
    expect(w.text()).toContain('参与打分')
  })

  it('renders top stock names after polling completes', async () => {
    const w = await mountAndWait()
    expect(w.text()).toContain('海康威视')
    expect(w.text()).toContain('五粮液')
    expect(w.text()).toContain('招商银行')
  })

  it('renders by-industry section', async () => {
    const w = await mountAndWait()
    expect(w.text()).toContain('分行业选股')
    expect(w.text()).toContain('银行')
    expect(w.text()).toContain('食品饮料')
  })

  it('shows factor detail panel placeholder', async () => {
    const w = await mountAndWait()
    expect(w.text()).toContain('因子明细')
    expect(w.text()).toContain('点击左侧表格行查看因子')
  })
})
