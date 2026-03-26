import { createRouter, createWebHistory } from 'vue-router'
import MainLayout from '../layouts/MainLayout.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      component: MainLayout,
      children: [
        { path: '', name: 'dashboard', component: () => import('../views/Dashboard.vue') },
        { path: 'data', name: 'data', component: () => import('../views/DataManage.vue') },
        { path: 'universe', redirect: '/watchlist' },
        { path: 'select', name: 'select', component: () => import('../views/StockSelect.vue') },
        { path: 'backtest', name: 'backtest', component: () => import('../views/Backtest.vue') },
        { path: 'paper', name: 'paper', component: () => import('../views/PaperTrading.vue') },
        { path: 'paper/replay', name: 'replay', component: () => import('../views/PaperReplay.vue') },
        { path: 'sentiment', name: 'sentiment', redirect: '/data' },
        { path: 'report', redirect: '/backtest' },
        { path: 'watchlist', name: 'watchlist', component: () => import('../views/Watchlist.vue') },
        { path: 'settings', name: 'settings', component: () => import('../views/Settings.vue') },
      ],
    },
  ],
})

export default router
