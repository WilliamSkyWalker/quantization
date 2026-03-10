import type { GlobalThemeOverrides } from 'naive-ui'

// ============================================================
// Unified Color Palette
// ============================================================

export const colors = {
  // Brand / Primary
  primary: '#2080f0',
  primaryHover: '#4098fc',
  primarySuppl: '#2080f0',
  primaryLight: 'rgba(32, 128, 240, 0.12)',

  // A-Share Market Convention: red=up, green=down
  up: '#cf222e',        // 涨 (gains)
  upBg: 'rgba(207, 34, 46, 0.08)',
  down: '#1a7f37',      // 跌 (losses)
  downBg: 'rgba(26, 127, 55, 0.08)',
  neutral: '#8b949e',   // 平

  // US Stock / General Semantic (green=positive, red=negative)
  positive: '#18a058',
  positiveBg: 'rgba(24, 160, 88, 0.08)',
  negative: '#d03050',
  negativeBg: 'rgba(208, 48, 80, 0.08)',

  // Functional
  success: '#18a058',
  warning: '#f0a020',
  error: '#d03050',
  info: '#2080f0',

  // K-line (A-share convention)
  klineUp: '#cf222e',
  klineDown: '#1a7f37',

  // Heatmap
  heatmapNeg: '#d03050',
  heatmapZero: '#ffffff',
  heatmapPos: '#18a058',

  // Text
  textPrimary: '#1f2328',
  textSecondary: '#656d76',
  textTertiary: '#8b949e',
  textDisabled: '#c0c4cc',

  // Backgrounds
  bgPage: '#f6f8fa',
  bgCard: '#ffffff',
  bgSidebar: '#1f2328',
  bgSidebarBorder: '#333',
  textSidebar: '#e0e0e0',
  bgHover: 'rgba(32, 128, 240, 0.06)',

  // Borders
  border: '#d0d7de',
  borderLight: '#e8ecf0',
  borderSubtle: '#f0f2f5',

  // Chart
  chartLine: '#2080f0',
  chartBenchmark: '#8b949e',
  chartDrawdown: '#d03050',
  chartDrawdownArea: 'rgba(208, 48, 80, 0.12)',

  // Stat card icon backgrounds
  statBlue: '#eff6ff',
  statGreen: '#f0fdf4',
  statRed: '#fef2f2',
  statOrange: '#fffbeb',

  // Tier colors (sentiment sources)
  tier1: '#d03050',
  tier2: '#f0a020',
  tier3: '#2080f0',
  tier4: '#18a058',
  tier5: '#8b949e',
  tier6: '#f59e0b',
  tier7: '#8b5cf6',
  tier8: '#3b82f6',
} as const

// ============================================================
// Naive UI Global Theme Overrides
// ============================================================

export const themeOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: colors.primary,
    primaryColorHover: colors.primaryHover,
    primaryColorSuppl: colors.primarySuppl,
    primaryColorPressed: '#1060c0',
    successColor: colors.success,
    warningColor: colors.warning,
    errorColor: colors.error,
    infoColor: colors.info,
    bodyColor: colors.bgPage,
    cardColor: colors.bgCard,
    textColorBase: colors.textPrimary,
    textColor1: colors.textPrimary,
    textColor2: colors.textSecondary,
    textColor3: colors.textTertiary,
    borderColor: colors.border,
    dividerColor: colors.borderLight,
    borderRadius: '8px',
    borderRadiusSmall: '6px',
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
  },
  Card: {
    borderRadius: '10px',
    paddingMedium: '20px',
    borderColor: colors.borderLight,
  },
  DataTable: {
    borderColor: colors.borderLight,
    thColor: colors.bgPage,
    tdColorStriped: '#fafbfc',
  },
  Tag: {
    borderRadius: '6px',
  },
  Button: {
    borderRadiusMedium: '8px',
    borderRadiusSmall: '6px',
    borderRadiusTiny: '4px',
  },
  Tabs: {
    tabFontWeightActive: '600',
    tabTextColorLine: colors.textSecondary,
    tabTextColorActiveLine: colors.primary,
    tabTextColorHoverLine: colors.primaryHover,
  },
  Progress: {
    fillColor: colors.primary,
  },
  Statistic: {
    labelFontSize: '13px',
    labelTextColor: colors.textTertiary,
  },
}

// ============================================================
// Sidebar Menu Theme Overrides
// ============================================================

export const sidebarMenuOverrides = {
  itemTextColor: 'rgba(255,255,255,0.65)',
  itemIconColor: 'rgba(255,255,255,0.65)',
  itemTextColorHover: '#fff',
  itemIconColorHover: '#fff',
  itemColorHover: 'rgba(255,255,255,0.09)',
  itemTextColorActive: '#fff',
  itemIconColorActive: colors.primary,
  itemTextColorActiveHover: '#fff',
  itemIconColorActiveHover: colors.primary,
  itemColorActive: `rgba(32, 128, 240, 0.15)`,
  itemColorActiveHover: `rgba(32, 128, 240, 0.15)`,
}

// ============================================================
// Helper: A-Share P&L color (red=up, green=down)
// ============================================================

export function pnlColor(val: number | null | undefined): string {
  if (val == null || val === 0) return colors.neutral
  return val > 0 ? colors.up : colors.down
}

// ============================================================
// Helper: General positive/negative color (green=good, red=bad)
// ============================================================

export function semanticColor(val: number | null | undefined): string {
  if (val == null || val === 0) return colors.neutral
  return val > 0 ? colors.positive : colors.negative
}
