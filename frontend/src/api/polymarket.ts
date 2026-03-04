import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    const msg = error.response?.data?.error || error.message || '请求失败'
    console.error(`[Polymarket API Error] ${error.config?.method?.toUpperCase()} ${error.config?.url}: ${msg}`)
    return Promise.reject(error)
  },
)

// Monitor control
export const startMonitor = () => api.post('/polymarket/monitor/start')
export const stopMonitor = () => api.post('/polymarket/monitor/stop')
export const getMonitorStatus = () => api.get('/polymarket/status')

// Alerts
export const getAlerts = (params: { page?: number; page_size?: number; is_read?: string } = {}) =>
  api.get('/polymarket/alerts', { params: { page: 1, page_size: 20, ...params } })
export const markAlertRead = (alertId: number) => api.post(`/polymarket/alerts/${alertId}/read`)

// Mock test
export const triggerMockAlert = (data: {
  question: string
  description?: string
  category?: string
  price_before: number
  price_after: number
  alert_type?: string
}) => api.post('/polymarket/mock-alert', data)

// Delete mock data
export const deleteMockAlerts = () => api.post('/polymarket/mock-alert/delete')

// Backtest
export const backtestDiscover = (data: { limit?: number; min_volume?: number } = {}) =>
  api.post('/polymarket/backtest/discover', data)

export const backtestDownload = (data: {
  condition_ids?: string[]
  limit?: number
  fidelity?: number
} = {}) => api.post('/polymarket/backtest/download', data)

export const getBacktestMarkets = (params: { page?: number; page_size?: number } = {}) =>
  api.get('/polymarket/backtest/markets', { params: { page: 1, page_size: 20, ...params } })

export const getBacktestPriceSeries = (conditionId: string) =>
  api.get(`/polymarket/backtest/price-series/${conditionId}`)

export const runBacktest = (data: {
  condition_ids?: string[]
  use_llm?: boolean
  spike_5m?: number
  spike_1h?: number
  spike_24h?: number
} = {}) => api.post('/polymarket/backtest/run', data)

// Impact Analysis
export const getImpactOverview = (params: { days?: number; min_confidence?: number } = {}) =>
  api.get('/polymarket/impact', { params: { days: 365, min_confidence: 0, ...params } })

// US Stock P&L Backtest
export const runUsStockPnl = (data: {
  alerts: any[]
  holding_days?: number
  min_confidence?: number
}) => api.post('/polymarket/backtest/us-stock-pnl', data)

export const runUsStockPnlFromDb = (data: {
  holding_days?: number
  min_confidence?: number
  limit?: number
}) => api.post('/polymarket/backtest/us-stock-pnl-from-db', data)
