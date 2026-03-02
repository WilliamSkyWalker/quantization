/**
 * DataManage.vue tests — table status, action buttons trigger API calls.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushAll, mountView } from './helpers'
import DataManage from '../views/DataManage.vue'
import { dataStatusResponse, taskSubmitResponse } from './fixtures'

vi.mock('../api')
import { getDataStatus, startDownload, startUpdate, startBackfillIncome, initDatabase } from '../api'

describe('DataManage.vue', () => {
  beforeEach(() => {
    vi.mocked(getDataStatus).mockResolvedValue({ data: dataStatusResponse } as any)
    vi.mocked(startDownload).mockResolvedValue({ data: taskSubmitResponse } as any)
    vi.mocked(startUpdate).mockResolvedValue({ data: { task_id: 'upd1', name: '增量更新' } } as any)
    vi.mocked(startBackfillIncome).mockResolvedValue({ data: { task_id: 'bf1', name: '回填利润表' } } as any)
    vi.mocked(initDatabase).mockResolvedValue({ data: { message: 'ok' } } as any)
  })

  it('loads data status on mount', async () => {
    mountView(DataManage)
    await flushAll()
    expect(getDataStatus).toHaveBeenCalledTimes(1)
  })

  it('renders table status rows', async () => {
    const w = mountView(DataManage)
    await flushAll()
    expect(w.text()).toContain('股票基本信息')
    expect(w.text()).toContain('日线行情')
    expect(w.text()).toContain('财务数据')
  })

  it('has action buttons', async () => {
    const w = mountView(DataManage)
    await flushAll()
    expect(w.text()).toContain('全量下载')
    expect(w.text()).toContain('增量更新')
    expect(w.text()).toContain('回填利润表')
    expect(w.text()).toContain('初始化数据库')
  })

  it('clicking 全量下载 calls startDownload', async () => {
    const w = mountView(DataManage)
    await flushAll()
    const btn = w.findAll('button').find(b => b.text().includes('全量下载'))
    expect(btn).toBeDefined()
    await btn!.trigger('click')
    await flushAll()
    expect(startDownload).toHaveBeenCalledWith('download_all')
  })

  it('clicking 增量更新 calls startUpdate', async () => {
    const w = mountView(DataManage)
    await flushAll()
    const btn = w.findAll('button').find(b => b.text().includes('增量更新'))
    await btn!.trigger('click')
    await flushAll()
    expect(startUpdate).toHaveBeenCalled()
  })

  it('clicking 回填利润表 calls startBackfillIncome', async () => {
    const w = mountView(DataManage)
    await flushAll()
    const btn = w.findAll('button').find(b => b.text().includes('回填利润表'))
    await btn!.trigger('click')
    await flushAll()
    expect(startBackfillIncome).toHaveBeenCalled()
  })
})
