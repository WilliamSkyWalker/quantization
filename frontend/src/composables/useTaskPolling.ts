import { ref, watch, onBeforeUnmount } from 'vue'
import { useMessage } from 'naive-ui'
import { useTaskStore } from '../stores/task'

interface UseTaskPollingOptions {
  taskLabel: string
}

export function useTaskPolling(options: UseTaskPollingOptions) {
  const message = useMessage()
  const taskStore = useTaskStore()
  const loading = ref(false)
  const taskId = ref('')
  const result = ref<any>(null)

  // Watch WebSocket-driven store updates
  const stopWatch = watch(
    () => taskId.value ? taskStore.tasks[taskId.value]?.status : undefined,
    (status) => {
      if (!taskId.value || !status) return
      if (status === 'completed') {
        result.value = taskStore.tasks[taskId.value]?.result
        loading.value = false
      } else if (status === 'failed' || status === 'cancelled') {
        const task = taskStore.tasks[taskId.value]
        message.error(`${options.taskLabel}失败: ${task?.error || '已取消'}`)
        loading.value = false
      }
    },
  )

  onBeforeUnmount(() => {
    stopWatch()
  })

  function start(id: string) {
    taskId.value = id
    result.value = null
    loading.value = true
  }

  function reset() {
    taskId.value = ''
    result.value = null
    loading.value = false
  }

  // Keep stopPolling in the API for backward compat (now a no-op)
  function stopPolling() {}

  return { loading, taskId, result, start, reset, stopPolling, taskStore }
}
