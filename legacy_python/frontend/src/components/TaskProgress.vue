<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useMessage, useDialog } from 'naive-ui'
import { CloseOutline, RefreshOutline, ReorderFourOutline } from '@vicons/ionicons5'
import { useTaskStore } from '../stores/task'
import { colors } from '../theme'

const message = useMessage()
const dialog = useDialog()
const taskStore = useTaskStore()
const expanded = ref(false)

onMounted(() => {
  taskStore.loadAllTasks()
})

function formatElapsed(sec?: number) {
  if (sec == null) return '-'
  if (sec < 60) return `${Math.round(sec)}秒`
  if (sec < 3600) return `${Math.floor(sec / 60)}分${Math.round(sec % 60)}秒`
  return `${Math.floor(sec / 3600)}时${Math.floor((sec % 3600) / 60)}分`
}

function statusType(status: string) {
  switch (status) {
    case 'running': return 'info'
    case 'completed': return 'success'
    case 'failed': return 'error'
    case 'cancelled': return 'default'
    case 'pending': return 'warning'
    default: return 'default'
  }
}

function statusLabel(status: string) {
  switch (status) {
    case 'running': return '运行中'
    case 'completed': return '已完成'
    case 'failed': return '失败'
    case 'cancelled': return '已取消'
    case 'pending': return '等待中'
    default: return status
  }
}

function kill(taskId: string, name: string) {
  dialog.warning({
    title: '终止任务',
    content: `确定要终止任务「${name}」吗？`,
    positiveText: '终止',
    negativeText: '取消',
    onPositiveClick: async () => {
      await taskStore.killTask(taskId)
      message.success('取消指令已发送')
    },
  })
}

function clearFinished() {
  taskStore.clearFinished()
}
</script>

<template>
  <!-- Floating trigger badge -->
  <div class="task-trigger" @click="expanded = !expanded">
    <n-badge :value="taskStore.activeTasks.length" :show="taskStore.hasActiveTasks" :max="9">
      <n-icon size="20"><ReorderFourOutline /></n-icon>
    </n-badge>
    <span class="trigger-text">任务</span>
  </div>

  <!-- Task panel -->
  <transition name="slide">
    <div v-show="expanded" class="task-panel">
      <div class="panel-header">
        <span class="panel-title">
          任务列表
          <n-tag size="small" type="info" v-if="taskStore.hasActiveTasks" style="margin-left: 8px">
            {{ taskStore.activeTasks.length }} 运行中
          </n-tag>
        </span>
        <n-space>
          <n-button text size="small" @click="taskStore.loadAllTasks">
            <template #icon><n-icon><RefreshOutline /></n-icon></template>
          </n-button>
          <n-button text size="small" @click="clearFinished" :disabled="taskStore.allTasks.length === taskStore.activeTasks.length">
            清除已完成
          </n-button>
          <n-button text size="small" @click="expanded = false">
            <template #icon><n-icon><CloseOutline /></n-icon></template>
          </n-button>
        </n-space>
      </div>

      <div class="panel-body">
        <div v-if="taskStore.allTasks.length === 0" class="empty-hint">暂无任务</div>

        <div v-for="task in taskStore.allTasks" :key="task.task_id" class="task-row">
          <div class="task-header">
            <div class="task-left">
              <n-tag :type="statusType(task.status)" size="small" style="min-width: 56px; text-align: center">
                {{ statusLabel(task.status) }}
              </n-tag>
              <span class="task-name">{{ task.name }}</span>
              <span class="task-id">{{ task.task_id }}</span>
            </div>
            <div class="task-right">
              <span class="task-elapsed">{{ formatElapsed(task.elapsed) }}</span>
              <n-button
                v-if="task.status === 'running' || task.status === 'pending'"
                type="error"
                size="small"
                secondary
                @click.stop="kill(task.task_id, task.name)"
              >
                终止
              </n-button>
            </div>
          </div>

          <!-- Progress bar for active tasks -->
          <div v-if="task.status === 'running' || task.status === 'pending'" class="task-progress">
            <n-progress
              type="line"
              :percentage="task.progress"
              :height="10"
              :show-indicator="false"
              style="flex: 1"
            />
            <span class="task-pct">{{ task.progress }}%</span>
          </div>

          <!-- Message -->
          <div class="task-message" v-if="task.message">
            <span v-if="task.status === 'failed'" :style="{ color: colors.error }">{{ task.message }}</span>
            <span v-else>{{ task.message }}</span>
          </div>
        </div>
      </div>
    </div>
  </transition>
</template>

<style scoped>
.task-trigger {
  position: fixed;
  bottom: 20px;
  right: 20px;
  z-index: 2001;
  background: #fff;
  border: 1px solid v-bind('colors.borderLight');
  border-radius: 24px;
  padding: 8px 16px;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.08);
  transition: box-shadow 0.2s;
  user-select: none;
}
.task-trigger:hover {
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
}
.trigger-text {
  font-size: 13px;
  color: v-bind('colors.textSecondary');
}

.task-panel {
  position: fixed;
  bottom: 64px;
  right: 20px;
  width: 520px;
  max-height: 480px;
  z-index: 2000;
  background: #fff;
  border: 1px solid v-bind('colors.borderLight');
  border-radius: 12px;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.1);
  display: flex;
  flex-direction: column;
}

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid v-bind('colors.borderLight');
}
.panel-title {
  font-size: 14px;
  font-weight: 600;
  color: v-bind('colors.textPrimary');
}

.panel-body {
  overflow-y: auto;
  padding: 8px 0;
  flex: 1;
}

.empty-hint {
  text-align: center;
  color: v-bind('colors.textDisabled');
  padding: 32px 0;
  font-size: 13px;
}

.task-row {
  padding: 10px 16px;
  border-bottom: 1px solid v-bind('colors.borderSubtle');
}
.task-row:last-child {
  border-bottom: none;
}

.task-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.task-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.task-name {
  font-size: 13px;
  color: v-bind('colors.textPrimary');
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.task-id {
  font-size: 11px;
  color: v-bind('colors.textDisabled');
  font-family: monospace;
}

.task-right {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.task-elapsed {
  font-size: 12px;
  color: v-bind('colors.textTertiary');
  white-space: nowrap;
}

.task-progress {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
}
.task-pct {
  font-size: 12px;
  color: v-bind('colors.textTertiary');
  min-width: 32px;
  text-align: right;
}

.task-message {
  font-size: 12px;
  color: v-bind('colors.textTertiary');
  margin-top: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* Slide transition */
.slide-enter-active, .slide-leave-active {
  transition: all 0.2s ease;
}
.slide-enter-from, .slide-leave-to {
  opacity: 0;
  transform: translateY(12px);
}
</style>
