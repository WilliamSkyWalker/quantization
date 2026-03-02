/**
 * Shared test helpers for mounting Vue components with router + pinia + naive-ui providers.
 */
import { mount, VueWrapper } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { getActivePinia, createPinia } from 'pinia'
import { type Component, h } from 'vue'
import {
  NConfigProvider,
  NMessageProvider,
  NDialogProvider,
  NNotificationProvider,
} from 'naive-ui'

const routes = [
  { path: '/', component: { template: '<div>home</div>' } },
  { path: '/data', component: { template: '<div>data</div>' } },
  { path: '/universe', component: { template: '<div>universe</div>' } },
  { path: '/select', component: { template: '<div>select</div>' } },
  { path: '/backtest', component: { template: '<div>backtest</div>' } },
  { path: '/paper', component: { template: '<div>paper</div>' } },
  { path: '/paper/replay', component: { template: '<div>replay</div>' } },
  { path: '/sentiment', component: { template: '<div>sentiment</div>' } },
  { path: '/report', component: { template: '<div>report</div>' } },
  { path: '/settings', component: { template: '<div>settings</div>' } },
]

/**
 * Wraps a component inside Naive UI providers so useMessage()/useDialog() work.
 */
function wrapWithProviders(component: Component, props?: Record<string, any>) {
  return {
    setup() {
      return () =>
        h(NConfigProvider, null, {
          default: () =>
            h(NMessageProvider, null, {
              default: () =>
                h(NDialogProvider, null, {
                  default: () =>
                    h(NNotificationProvider, null, {
                      default: () => h(component, props),
                    }),
                }),
            }),
        })
    },
  }
}

export function mountView(component: Component, options: Record<string, any> = {}): VueWrapper {
  const router = createRouter({ history: createMemoryHistory(), routes })
  // Reuse the active Pinia from setup.ts so test-created stores share state with the component
  const pinia = getActivePinia() || createPinia()

  const wrapped = wrapWithProviders(component, options.props)

  return mount(wrapped, {
    global: {
      plugins: [router, pinia],
      stubs: {
        VChart: { template: '<div class="echarts-stub" />' },
      },
    },
    attachTo: document.body,
  }) as VueWrapper
}

/** Flush microtasks + nextTick — enough depth for Promise.allSettled chains */
export async function flushAll() {
  for (let i = 0; i < 5; i++) {
    await new Promise(r => setTimeout(r, 0))
  }
}
