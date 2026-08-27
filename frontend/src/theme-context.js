/**
 * The theme context object and the hooks that read it.
 *
 * Separate from ThemeProvider.jsx on purpose. Vite's Fast Refresh can only
 * hot-swap a module whose exports are all components; a file that exports both
 * <ThemeProvider> and useTheme() forces a full page reload on every edit, and
 * `react-refresh/only-export-components` flags exactly that. Hooks here,
 * component there, and editing either one refreshes in place.
 *
 * The palette itself is in theme.js - this module only carries it around.
 */

import { createContext, useContext } from 'react'

import { CHART_COLORS, HEATMAP_RAMP, LIGHT } from './theme'

export const ThemeContext = createContext(null)

/**
 * A complete, working light-mode value for when there is no provider above.
 *
 * A missing provider should cost you the toggle, not the dashboard - and it
 * keeps every consuming component renderable on its own.
 */
const FALLBACK = {
  theme: LIGHT,
  isDark: false,
  setTheme: () => {},
  toggleTheme: () => {},
  chart: CHART_COLORS[LIGHT],
  heatmapRamp: HEATMAP_RAMP[LIGHT],
}

/** The current theme, plus `setTheme` / `toggleTheme` to change it. */
export function useTheme() {
  return useContext(ThemeContext) ?? FALLBACK
}

/** Just the Recharts palette - all the chart components need. */
export function useChartColors() {
  return useTheme().chart
}
