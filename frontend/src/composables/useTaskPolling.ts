import { ref, watch, onBeforeUnmount } from 'vue'
import { useMessage } from 'naive-ui'
import { useTaskStore } from '../stores/task'

interface UseTaskPollingOptions {
  fetchResult: (taskId: string) => Promise<any>
  taskLabel: string
}

export function useTaskPolling(options: UseTaskPollingOptions) {
  const message = useMessage()
  const taskStore = useTaskStore()
  const loading = ref(false)
  const taskId = ref('')
  const result = ref<any>(null)
  let pollTimer: ReturnType<typeof setInterval> | null = null

  function startPolling() {
    pollTimer = setInterval(async () => {
      if (!taskId.value) return
      try {
        const { data } = await options.fetchResult(taskId.value)
        taskStore.tasks[taskId.value] = data
        if (data.status === 'completed') {
          result.value = data.result
          loading.value = false
          stopPolling()
        } else if (data.status === 'failed' || data.status === 'cancelled') {
          message.error(`${options.taskLabel}失败: ${data.error || '已取消'}`)
          loading.value = false
          stopPolling()
        }
      } catch {
        // transient error, keep polling
      }
    }, 2000)
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  // Watch WebSocket updates for fast completion
  const stopWatch = watch(
    () => taskId.value ? taskStore.tasks[taskId.value]?.status : undefined,
    async (status) => {
      if (!taskId.value || !status) return
      if (status === 'completed') {
        if (!result.value) {
          try {
            const { data } = await options.fetchResult(taskId.value)
            result.value = data.result
          } catch {
            result.value = taskStore.tasks[taskId.value]?.result
          }
        }
        loading.value = false
        stopPolling()
      } else if (status === 'failed' || status === 'cancelled') {
        const task = taskStore.tasks[taskId.value]
        message.error(`${options.taskLabel}失败: ${task?.error || '已取消'}`)
        loading.value = false
        stopPolling()
      }
    },
  )

  // Cleanup on unmount
  onBeforeUnmount(() => {
    stopPolling()
    stopWatch()
  })

  function start(id: string) {
    taskId.value = id
    result.value = null
    loading.value = true
    startPolling()
  }

  function reset() {
    stopPolling()
    taskId.value = ''
    result.value = null
    loading.value = false
  }

  return { loading, taskId, result, start, reset, stopPolling, taskStore }
}
