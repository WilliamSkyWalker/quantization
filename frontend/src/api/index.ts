import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    const msg = error.response?.data?.error || error.message || '请求失败'
    console.error(`[API Error] ${error.config?.method?.toUpperCase()} ${error.config?.url}: ${msg}`)
    return Promise.reject(error)
  },
)

// Data management
export const getDataStatus = () => api.get('/data/status')
export const startDownload = (action: string) => api.post('/data/download', { action })
export const startUpdate = () => api.post('/data/update')
export const startBackfillIncome = () => api.post('/data/backfill-income')
export const browseData = (table: string, params: { page?: number; page_size?: number; keyword?: string } = {}) =>
  api.get('/data/browse', { params: { table, page: 1, ...params } })

// Tasks
export const getTaskList = () => api.get('/tasks/')
export const getTaskStatus = (taskId: string) => api.get(`/tasks/${taskId}`)
export const cancelTask = (taskId: string) => api.post(`/tasks/${taskId}/cancel`)

// Stock pool & selection
export const getUniverse = (date?: string) => api.get('/universe', { params: { date } })
export const startSelectStocks = (date?: string) => api.post('/select', { date })
export const getSelectHistory = () => api.get('/select/history')
export const getSelectHistoryDate = (date: string) => api.get(`/select/history/${date}`)
export const getFactorDetail = (date: string, code: string) =>
  api.get('/factors', { params: { date, code } })

// Backtest
export const startBacktest = (startDate: string, endDate: string) =>
  api.post('/backtest/run', { start_date: startDate, end_date: endDate })

// Paper trading
export const getPaperAccount = () => api.get('/paper/account')
export const getPaperPositions = () => api.get('/paper/positions')
export const getPaperNav = (days?: number) => api.get('/paper/nav', { params: { days } })
export const getPaperTransactions = (last?: number, date?: string) =>
  api.get('/paper/transactions', { params: { last, date } })
export const startPaperTrade = () => api.post('/paper/trade')
export const startPaperReplay = (startDate: string, endDate: string, reset = false, capital?: number) =>
  api.post('/paper/replay', { start_date: startDate, end_date: endDate, reset, capital })
export const resetPaper = () => api.post('/paper/reset')

// Sentiment
export const getSentimentStatus = () => api.get('/sentiment/status')
export const getSentimentArticles = (params: {
  source?: string
  category?: string
  start_date?: string
  end_date?: string
  keyword?: string
  page?: number
  page_size?: number
} = {}) => api.get('/sentiment/articles', { params: { page: 1, ...params } })
export const startSentimentDownload = (source?: string, tier?: number, incremental?: boolean, backfill?: boolean) =>
  api.post('/sentiment/download', { source, tier, incremental, backfill })
export const startSentimentAnalyze = () => api.post('/sentiment/analyze')
export const startSentimentBackfillAnalyze = () => api.post('/sentiment/backfill-analyze')
export const startSentimentBackfillContent = (source?: string) =>
  api.post('/sentiment/backfill-content', { source })
export const startSentimentBackfillLLM = () => api.post('/sentiment/backfill-llm')
export const getSentimentAnalysisStats = () => api.get('/sentiment/analysis-stats')
export const startSentimentDownloadAndAnalyze = (source?: string) =>
  api.post('/sentiment/download-and-analyze', { source })

// Config
export const getSettings = () => api.get('/config/settings')
export const updateSettings = (data: Record<string, any>) => api.put('/config/settings/update', data)
export const getIndustryFactors = () => api.get('/config/industry-factors')
export const getAllIndustries = () => api.get('/config/industries')
export const updateIndustryFactors = (data: Record<string, any>) =>
  api.put('/config/industry-factors/update', data)
export const initDatabase = () => api.post('/config/init')

// Stock detail
export const searchStocks = (q: string) => api.get('/stock/search', { params: { q } })
export const getStockProfile = (tsCode: string) => api.get(`/stock/${tsCode}/profile`)
export const getStockKline = (tsCode: string, startDate?: string, endDate?: string) =>
  api.get(`/stock/${tsCode}/kline`, { params: { start_date: startDate, end_date: endDate } })
export const getStockReports = (tsCode: string, page = 1) =>
  api.get(`/stock/${tsCode}/reports`, { params: { page } })
export const getStockNews = (tsCode: string, page = 1) =>
  api.get(`/stock/${tsCode}/news`, { params: { page } })

export default api
