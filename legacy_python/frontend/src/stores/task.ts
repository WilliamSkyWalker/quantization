import { defineStore } from 'pinia'
import { reactive, computed } from 'vue'
import { getTaskList, cancelTask } from '../api'

export interface TaskInfo {
  task_id: string
  name: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number
  message: string
  result: any
  error: string
  created_at?: number
  started_at?: number
  finished_at?: number
  elapsed?: number
}

export const useTaskStore = defineStore('task', () => {
  // Use a reactive plain object instead of Map for Vue reactivity
  const tasks = reactive<Record<string, TaskInfo>>({})
  let ws: WebSocket | null = null

  const allTasks = computed(() =>
    Object.values(tasks).sort((a, b) => (b.created_at || 0) - (a.created_at || 0))
  )

  const activeTasks = computed(() =>
    allTasks.value.filter(t => t.status === 'running' || t.status === 'pending')
  )

  const hasActiveTasks = computed(() => activeTasks.value.length > 0)

  let retryDelay = 3000
  const MAX_RETRY_DELAY = 30000

  function connectWebSocket() {
    if (ws?.readyState === WebSocket.OPEN) return

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const host = window.location.host

    try {
      ws = new WebSocket(`${protocol}//${host}/ws/tasks/`)
    } catch {
      scheduleReconnect()
      return
    }

    ws.onopen = () => {
      retryDelay = 3000
      loadAllTasks()
    }

    ws.onmessage = (event) => {
      const data: TaskInfo = JSON.parse(event.data)
      tasks[data.task_id] = data
    }

    ws.onclose = () => {
      scheduleReconnect()
    }

    ws.onerror = () => {}
  }

  function scheduleReconnect() {
    setTimeout(connectWebSocket, retryDelay)
    retryDelay = Math.min(retryDelay * 1.5, MAX_RETRY_DELAY)
  }

  function trackTask(taskId: string, name: string) {
    tasks[taskId] = {
      task_id: taskId,
      name,
      status: 'pending',
      progress: 0,
      message: '等待中...',
      result: null,
      error: '',
      created_at: Date.now() / 1000,
    }
  }

  async function loadAllTasks() {
    try {
      const { data } = await getTaskList()
      for (const t of data) {
        tasks[t.task_id] = t
      }
    } catch {
      // backend not ready
    }
  }

  async function killTask(taskId: string) {
    await cancelTask(taskId)
    // Optimistic update
    const t = tasks[taskId]
    if (t) {
      t.status = 'cancelled'
      t.message = '正在取消...'
    }
  }

  function getTask(taskId: string): TaskInfo | undefined {
    return tasks[taskId]
  }

  function removeTask(taskId: string) {
    delete tasks[taskId]
  }

  function clearFinished() {
    for (const id of Object.keys(tasks)) {
      const t = tasks[id]
      if (t && (t.status === 'completed' || t.status === 'failed' || t.status === 'cancelled')) {
        delete tasks[id]
      }
    }
  }

  return {
    tasks,
    allTasks,
    activeTasks,
    hasActiveTasks,
    connectWebSocket,
    trackTask,
    loadAllTasks,
    killTask,
    getTask,
    removeTask,
    clearFinished,
  }
})
