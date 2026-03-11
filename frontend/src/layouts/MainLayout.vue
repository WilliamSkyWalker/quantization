<script setup lang="ts">
import { h, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NIcon } from 'naive-ui'
import type { MenuOption } from 'naive-ui'
import { useAppStore } from '../stores/app'
import TaskProgress from '../components/TaskProgress.vue'
import { colors, sidebarMenuOverrides } from '../theme'
import {
  SpeedometerOutline,
  DownloadOutline,
  SearchOutline,
  TrendingUpOutline,
  AnalyticsOutline,
  WalletOutline,
  SettingsOutline,
  MenuOutline,
} from '@vicons/ionicons5'

const route = useRoute()
const router = useRouter()
const appStore = useAppStore()

function renderIcon(icon: any) {
  return () => h(NIcon, null, { default: () => h(icon) })
}

const menuOptions: MenuOption[] = [
  { label: '仪表盘', key: '/', icon: renderIcon(SpeedometerOutline) },
  { label: '数据管理', key: '/data', icon: renderIcon(DownloadOutline) },
  { label: '个股详情', key: '/universe', icon: renderIcon(SearchOutline) },
  { label: '今日选股', key: '/select', icon: renderIcon(TrendingUpOutline) },
  { label: '回测', key: '/backtest', icon: renderIcon(AnalyticsOutline) },
  { label: '模拟交易', key: '/paper', icon: renderIcon(WalletOutline) },
  { label: '系统设置', key: '/settings', icon: renderIcon(SettingsOutline) },
]

const breadcrumbLabel = computed(() => {
  const item = menuOptions.find(m => m.key === route.path)
  return (item?.label as string) || '仪表盘'
})

function handleMenuUpdate(key: string) {
  router.push(key)
}
</script>

<template>
  <n-layout has-sider style="height: 100vh">
    <!-- Sidebar -->
    <n-layout-sider
      bordered
      :collapsed="appStore.sidebarCollapsed"
      collapse-mode="width"
      :collapsed-width="64"
      :width="220"
      :native-scrollbar="false"
      show-trigger="bar"
      @collapse="appStore.sidebarCollapsed = true"
      @expand="appStore.sidebarCollapsed = false"
      content-style="padding: 0;"
      :style="{ background: colors.bgSidebar }"
    >
      <div class="logo">
        <n-icon size="24" :color="colors.primary"><TrendingUpOutline /></n-icon>
        <span v-show="!appStore.sidebarCollapsed" class="logo-text">量化系统</span>
      </div>
      <n-menu
        :options="menuOptions"
        :value="route.path"
        :collapsed="appStore.sidebarCollapsed"
        :collapsed-width="64"
        :collapsed-icon-size="22"
        @update:value="handleMenuUpdate"
        :root-indent="18"
        :indent="18"
        :theme-overrides="sidebarMenuOverrides"
      />
    </n-layout-sider>

    <!-- Main -->
    <n-layout>
      <n-layout-header bordered class="header">
        <div class="header-left">
          <n-icon class="collapse-btn" size="20" @click="appStore.toggleSidebar">
            <MenuOutline />
          </n-icon>
          <n-breadcrumb>
            <n-breadcrumb-item @click="router.push('/')">首页</n-breadcrumb-item>
            <n-breadcrumb-item v-if="route.path !== '/'">{{ breadcrumbLabel }}</n-breadcrumb-item>
          </n-breadcrumb>
        </div>
      </n-layout-header>

      <n-layout-content content-style="padding: 20px;" class="main-content">
        <router-view />
      </n-layout-content>
    </n-layout>

    <!-- Global task progress -->
    <TaskProgress />
  </n-layout>
</template>

<style scoped>
.logo {
  height: 50px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  border-bottom: 1px solid v-bind('colors.bgSidebarBorder');
}

.logo-text {
  color: v-bind('colors.textSidebar');
  font-size: 16px;
  font-weight: 600;
  white-space: nowrap;
}

.header {
  height: 50px;
  background: #fff;
  display: flex;
  align-items: center;
  padding: 0 20px;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 16px;
}

.collapse-btn {
  cursor: pointer;
  color: v-bind('colors.textTertiary');
  transition: color 0.2s;
}
.collapse-btn:hover {
  color: v-bind('colors.primary');
}

.main-content {
  background: v-bind('colors.bgPage');
  height: calc(100vh - 50px);
  overflow-y: auto;
}
</style>
