/**
 * Task store tests — verify reactive state, polling, cancel.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useTaskStore } from '../stores/task'

// Mock API module
vi.mock('../api', () => ({
  getTaskList: vi.fn().mockResolvedValue({
    data: [
      { task_id: 't1', name: '下载', status: 'running', progress: 50, message: '下载中...', result: null, error: '', created_at: 100 },
      { task_id: 't2', name: '回测', status: 'completed', progress: 100, message: '完成', result: {}, error: '', created_at: 90 },
    ],
  }),
  getTaskStatus: vi.fn().mockResolvedValue({
    data: { task_id: 't1', name: '下载', status: 'completed', progress: 100, message: '完成', result: { count: 5000 }, error: '' },
  }),
  cancelTask: vi.fn().mockResolvedValue({ data: { message: '已取消' } }),
}))

describe('TaskStore', () => {
  let store: ReturnType<typeof useTaskStore>

  beforeEach(() => {
    store = useTaskStore()
  })

  it('trackTask adds a pending task', () => {
    store.trackTask('x1', '测试任务')
    expect(store.tasks['x1']).toBeDefined()
    expect(store.tasks['x1'].status).toBe('pending')
    expect(store.tasks['x1'].name).toBe('测试任务')
  })

  it('activeTasks filters running/pending only', () => {
    store.trackTask('a1', '任务A')
    store.tasks['a1'].status = 'running'
    store.trackTask('a2', '任务B')
    store.tasks['a2'].status = 'completed'
    store.trackTask('a3', '任务C')
    // a3 is pending by default

    expect(store.activeTasks.length).toBe(2) // a1 (running) + a3 (pending)
    expect(store.hasActiveTasks).toBe(true)
  })

  it('loadAllTasks populates from API', async () => {
    await store.loadAllTasks()
    expect(store.tasks['t1']).toBeDefined()
    expect(store.tasks['t1'].status).toBe('running')
    expect(store.tasks['t2']).toBeDefined()
    expect(store.tasks['t2'].status).toBe('completed')
  })

  it('pollTask fetches updated status', async () => {
    store.trackTask('t1', '下载')
    const result = await store.pollTask('t1')
    expect(result.status).toBe('completed')
    expect(result.result).toEqual({ count: 5000 })
    // Store also updated
    expect(store.tasks['t1'].status).toBe('completed')
  })

  it('killTask calls cancel API and updates local state', async () => {
    store.trackTask('t1', '下载')
    store.tasks['t1'].status = 'running'
    await store.killTask('t1')
    expect(store.tasks['t1'].status).toBe('cancelled')
  })

  it('removeTask deletes from store', () => {
    store.trackTask('r1', '临时')
    expect(store.tasks['r1']).toBeDefined()
    store.removeTask('r1')
    expect(store.tasks['r1']).toBeUndefined()
  })

  it('clearFinished removes completed/failed/cancelled tasks', () => {
    store.trackTask('c1', '完成')
    store.tasks['c1'].status = 'completed'
    store.trackTask('c2', '失败')
    store.tasks['c2'].status = 'failed'
    store.trackTask('c3', '取消')
    store.tasks['c3'].status = 'cancelled'
    store.trackTask('c4', '运行中')
    store.tasks['c4'].status = 'running'

    store.clearFinished()
    expect(store.tasks['c1']).toBeUndefined()
    expect(store.tasks['c2']).toBeUndefined()
    expect(store.tasks['c3']).toBeUndefined()
    expect(store.tasks['c4']).toBeDefined()
  })

  it('allTasks sorts by created_at descending', () => {
    store.trackTask('s1', '旧')
    store.tasks['s1'].created_at = 100
    store.trackTask('s2', '新')
    store.tasks['s2'].created_at = 200

    expect(store.allTasks[0].task_id).toBe('s2')
    expect(store.allTasks[1].task_id).toBe('s1')
  })

  it('getTask returns undefined for non-existent task', () => {
    expect(store.getTask('nonexistent')).toBeUndefined()
  })
})
