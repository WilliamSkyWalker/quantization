/**
 * Axios mock helper.
 * Call mockApi() in each test to get a vi.fn()-based mock of the default axios instance.
 * Returns the mock so tests can configure per-request responses.
 */
import { vi } from 'vitest'
import type { AxiosResponse } from 'axios'

/** Wrap data in an Axios-like response envelope. */
function axiosResp<T>(data: T, status = 200): AxiosResponse<T> {
  return { data, status, statusText: 'OK', headers: {}, config: {} as any }
}

/**
 * Mock every named export from `../api/index.ts`.
 * Returns an object keyed by export name → vi.fn() that resolves the given fixture.
 *
 * Usage:
 *   const mocks = setupApiMocks({ getDataStatus: dataStatusResponse })
 *   // Now getDataStatus() in component code returns the fixture.
 */
export function setupApiMocks(overrides: Record<string, any> = {}) {
  const mocks: Record<string, ReturnType<typeof vi.fn>> = {}

  for (const [key, value] of Object.entries(overrides)) {
    mocks[key] = vi.fn().mockResolvedValue(axiosResp(value))
  }

  // vi.mock is hoisted, so we do dynamic mock inside the test file.
  // This helper just creates the fns; the test file wires them with vi.mock.
  return mocks
}

export { axiosResp }
