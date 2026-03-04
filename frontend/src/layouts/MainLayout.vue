<script setup lang="ts">
import { h, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NIcon } from 'naive-ui'
import type { MenuOption } from 'naive-ui'
import { useAppStore } from '../stores/app'
import TaskProgress from '../components/TaskProgress.vue'
import {
  SpeedometerOutline,
  DownloadOutline,
  SearchOutline,
  TrendingUpOutline,
  AnalyticsOutline,
  WalletOutline,
  SettingsOutline,
  MenuOutline,
  PulseOutline,
  BarChartOutline,
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
  { label: 'Polymarket', key: '/polymarket', icon: renderIcon(PulseOutline) },
  { label: '美股回测', key: '/us-backtest', icon: renderIcon(BarChartOutline) },
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
      style="background: #252627"
    >
      <div class="logo">
        <n-icon size="24" color="#409eff"><TrendingUpOutline /></n-icon>
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
        :theme-overrides="{
          itemTextColor: 'rgba(255,255,255,0.65)',
          itemIconColor: 'rgba(255,255,255,0.65)',
          itemTextColorHover: '#fff',
          itemIconColorHover: '#fff',
          itemColorHover: 'rgba(255,255,255,0.09)',
          itemTextColorActive: '#fff',
          itemIconColorActive: '#409eff',
          itemTextColorActiveHover: '#fff',
          itemIconColorActiveHover: '#409eff',
          itemColorActive: 'rgba(64,158,255,0.15)',
          itemColorActiveHover: 'rgba(64,158,255,0.15)',
        }"
      />
    </n-layout-sider>

    <!-- Main -->
    <n-layout>
      <n-layout-header bordered style="height: 50px; background: #fff; display: flex; align-items: center; padding: 0 20px">
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

      <n-layout-content content-style="padding: 20px;" style="background: #f5f7fa; height: calc(100vh - 50px); overflow-y: auto">
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
  border-bottom: 1px solid #333;
}

.logo-text {
  color: #e0e0e0;
  font-size: 16px;
  font-weight: 600;
  white-space: nowrap;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 16px;
}

.collapse-btn {
  cursor: pointer;
  color: #666;
}
.collapse-btn:hover {
  color: #409eff;
}
</style>
