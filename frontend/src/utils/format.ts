/**
 * Format a timestamp (milliseconds) to YYYY-MM-DD string.
 */
export function formatDate(ts: number): string {
  const d = new Date(ts)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/**
 * Get today's date as YYYY-MM-DD string.
 */
export function todayStr(): string {
  return formatDate(Date.now())
}
