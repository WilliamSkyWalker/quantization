/**
 * Backtest.vue tests — starts backtest, polls for result, renders charts.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mountView } from './helpers'
import Backtest from '../views/Backtest.vue'
import { taskCompletedResponse } from './fixtures'

vi.mock('../api')
import { startBacktest, getBacktestResult } from '../api'

// Use real timers for flushAll, fake only for poll intervals
async function flush() {
  await vi.runAllTimersAsync()
  await new Promise(r => queueMicrotask(r))
  await vi.runAllTimersAsync()
}

describe('Backtest.vue', () => {
  beforeEach(() => {
    vi.mocked(startBacktest).mockResolvedValue({ data: { task_id: 'bt1', name: '回测' } } as any)
    vi.mocked(getBacktestResult).mockResolvedValue({ data: taskCompletedResponse } as any)
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders run button', async () => {
    const w = mountView(Backtest)
    await flush()
    expect(w.text()).toContain('运行回测')
  })

  it('shows empty state before running', async () => {
    const w = mountView(Backtest)
    await flush()
    expect(w.text()).toContain('选择日期范围并点击运行回测')
  })

  it('clicking run calls startBacktest API', async () => {
    const w = mountView(Backtest)
    await flush()
    const runBtn = w.findAll('button').find(b => b.text().includes('运行回测'))
    expect(runBtn).toBeDefined()
    await runBtn!.trigger('click')
    await flush()
    expect(startBacktest).toHaveBeenCalledWith('2020-01-01', '2024-12-31')
  })

  it('polls and shows result after completion', async () => {
    const w = mountView(Backtest)
    await flush()

    // Start backtest
    const runBtn = w.findAll('button').find(b => b.text().includes('运行回测'))
    await runBtn!.trigger('click')
    await flush()

    // Advance past the 2s poll interval
    await vi.advanceTimersByTimeAsync(2500)
    await flush()

    expect(getBacktestResult).toHaveBeenCalledWith('bt1')
    // Should show summary metrics from completed result
    expect(w.text()).toContain('125.3%')
    expect(w.text()).toContain('18.5%')
  })
})
