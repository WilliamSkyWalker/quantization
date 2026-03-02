/**
 * TaskProgress.vue tests — trigger badge, task panel, kill button.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { nextTick } from 'vue'
import { flushAll, mountView } from './helpers'
import TaskProgress from '../components/TaskProgress.vue'
import { useTaskStore } from '../stores/task'

vi.mock('../api')
import { getTaskList } from '../api'

describe('TaskProgress.vue', () => {
  beforeEach(() => {
    vi.mocked(getTaskList).mockResolvedValue({ data: [] } as any)
  })

  it('renders trigger badge with 任务 text', async () => {
    const w = mountView(TaskProgress)
    await flushAll()
    expect(w.find('.task-trigger').exists()).toBe(true)
    expect(w.text()).toContain('任务')
  })

  it('shows 暂无任务 when panel is expanded and no tasks exist', async () => {
    const w = mountView(TaskProgress)
    await flushAll()
    // Click to expand
    await w.find('.task-trigger').trigger('click')
    await nextTick()
    // Check panel content
    expect(w.text()).toContain('暂无任务')
    expect(w.text()).toContain('任务列表')
  })

  it('shows active task details in panel', async () => {
    const store = useTaskStore()
    store.trackTask('x1', '测试任务')
    store.tasks['x1'].status = 'running'
    store.tasks['x1'].progress = 42
    store.tasks['x1'].message = '处理中...'

    const w = mountView(TaskProgress)
    await flushAll()
    await w.find('.task-trigger').trigger('click')
    await nextTick()

    expect(w.text()).toContain('测试任务')
    expect(w.text()).toContain('42%')
  })

  it('shows 终止 button for running tasks', async () => {
    const store = useTaskStore()
    store.trackTask('x1', '运行中任务')
    store.tasks['x1'].status = 'running'

    const w = mountView(TaskProgress)
    await flushAll()
    await w.find('.task-trigger').trigger('click')
    await nextTick()

    const killBtn = w.findAll('button').find(b => b.text().includes('终止'))
    expect(killBtn).toBeDefined()
  })

  it('does not show 终止 button for completed tasks', async () => {
    const store = useTaskStore()
    store.trackTask('x1', '已完成任务')
    store.tasks['x1'].status = 'completed'

    const w = mountView(TaskProgress)
    await flushAll()
    await w.find('.task-trigger').trigger('click')
    await nextTick()

    const killBtn = w.findAll('button').find(b => b.text().includes('终止'))
    expect(killBtn).toBeUndefined()
  })

  it('has 清除已完成 button', async () => {
    const store = useTaskStore()
    store.trackTask('x1', '已完成')
    store.tasks['x1'].status = 'completed'

    const w = mountView(TaskProgress)
    await flushAll()
    await w.find('.task-trigger').trigger('click')
    await nextTick()

    expect(w.text()).toContain('清除已完成')
  })
})
