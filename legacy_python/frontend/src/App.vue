<script setup lang="ts">
import { onMounted } from 'vue'
import { zhCN, dateZhCN } from 'naive-ui'
import { useTaskStore } from './stores/task'
import { themeOverrides } from './theme'

const taskStore = useTaskStore()

onMounted(() => {
  taskStore.connectWebSocket()
})
</script>

<template>
  <n-config-provider :locale="zhCN" :date-locale="dateZhCN" :theme-overrides="themeOverrides">
    <n-message-provider>
      <n-dialog-provider>
        <n-notification-provider>
          <router-view />
        </n-notification-provider>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>
