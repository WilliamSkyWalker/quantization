/**
 * StockPool.vue tests — loads universe, renders stats and stock table.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushAll, mountView } from './helpers'
import StockPool from '../views/StockPool.vue'
import { universeResponse } from './fixtures'

vi.mock('../api')
import { getUniverse } from '../api'

describe('StockPool.vue', () => {
  beforeEach(() => {
    vi.mocked(getUniverse).mockResolvedValue({ data: universeResponse } as any)
  })

  it('calls getUniverse on mount', async () => {
    mountView(StockPool)
    await flushAll()
    expect(getUniverse).toHaveBeenCalled()
  })

  it('renders pool summary stats', async () => {
    const w = mountView(StockPool)
    await flushAll()
    expect(w.text()).toContain('4200')  // total
    expect(w.text()).toContain('35')    // limit_up
    expect(w.text()).toContain('12')    // limit_down
  })

  it('renders stock table with stock data', async () => {
    const w = mountView(StockPool)
    await flushAll()
    expect(w.text()).toContain('平安银行')
    expect(w.text()).toContain('000001.SZ')
    expect(w.text()).toContain('贵州茅台')
  })

  it('renders industry distribution section', async () => {
    const w = mountView(StockPool)
    await flushAll()
    expect(w.text()).toContain('行业分布')
  })
})
