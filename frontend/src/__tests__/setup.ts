/**
 * Global test setup:
 * - Stub ECharts (heavy, not needed in unit tests)
 * - Provide ResizeObserver polyfill for happy-dom
 */
import { vi } from 'vitest'
import { config } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import naive from 'naive-ui'

// Mock vue-echarts module to avoid importing real echarts/zrender
vi.mock('vue-echarts', () => ({
  default: { template: '<div class="echarts-stub" />', props: ['option', 'autoresize'] },
}))

// Mock echarts modules to prevent heavy imports and canvas errors
vi.mock('echarts/core', () => ({ use: () => {} }))
vi.mock('echarts/charts', () => ({
  LineChart: 'LineChart',
  BarChart: 'BarChart',
  PieChart: 'PieChart',
  HeatmapChart: 'HeatmapChart',
}))
vi.mock('echarts/components', () => ({
  GridComponent: 'GridComponent',
  TooltipComponent: 'TooltipComponent',
  LegendComponent: 'LegendComponent',
  DataZoomComponent: 'DataZoomComponent',
  VisualMapComponent: 'VisualMapComponent',
  ToolboxComponent: 'ToolboxComponent',
  TitleComponent: 'TitleComponent',
  MarkLineComponent: 'MarkLineComponent',
}))
vi.mock('echarts/renderers', () => ({
  CanvasRenderer: 'CanvasRenderer',
}))

// Stub vue-echarts globally (renders as empty div)
config.global.stubs = {
  VChart: { template: '<div class="echarts-stub" />' },
}

// Register Naive UI components globally for all tests
config.global.plugins = [naive]

// Reset Pinia before each test
beforeEach(() => {
  setActivePinia(createPinia())
})

// ResizeObserver polyfill (happy-dom doesn't have it)
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as any
}

// Stub WebSocket
class MockWebSocket {
  static OPEN = 1
  readyState = 0
  onopen: (() => void) | null = null
  onmessage: ((e: any) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  close() {}
  send() {}
}
globalThis.WebSocket = MockWebSocket as any
