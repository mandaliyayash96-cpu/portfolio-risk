/**
 * Owns the theme, and writes it onto <html>.
 *
 * Context rather than props because the two ends of this are far apart: the
 * toggle lives in <Header>, and the components that need the answer are three
 * charts and a heatmap nested under other panels. Threading a `theme` prop plus
 * a setter through App -> panel -> chart would touch every component in
 * between, none of which care.
 *
 * The provider does exactly one side effect: set `data-theme` on <html>. Every
 * painted colour follows from that attribute through the CSS custom properties
 * in index.css, so flipping the theme is one attribute write and a style
 * recalculation - not a re-render of the dashboard. Only the charts re-render,
 * because SVG cannot be themed by CSS variables alone.
 *
 * The hooks that read this live in theme-context.js; see the note there.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import { ThemeContext } from './theme-context'
import {
  CHART_COLORS,
  DARK,
  DARK_QUERY,
  HEATMAP_RAMP,
  LIGHT,
  readStoredTheme,
  resolveInitialTheme,
  storeTheme,
} from './theme'

export function ThemeProvider({ children }) {
  // Resolved synchronously on first render, not in an effect. The inline script
  // in index.html has already stamped the attribute to avoid a flash of the
  // wrong theme; starting from the same answer keeps React's first paint
  // agreeing with what is already on screen.
  const [theme, setThemeState] = useState(resolveInitialTheme)

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    // Themes the browser's OWN widgets - <select> popups, scrollbars, focus
    // rings. Without it the dropdowns in the alert rule form stay light-on-light
    // in dark mode, and no amount of CSS on our side reaches them.
    document.documentElement.style.colorScheme = theme
  }, [theme])

  // Follow the OS while - and only while - the user has never chosen. Once a
  // preference is stored it outranks the system in both directions: someone who
  // picked light and then puts their laptop into dark mode at dusk has not
  // asked for this dashboard to change.
  useEffect(() => {
    if (readStoredTheme() !== null) return undefined

    let media
    try {
      media = window.matchMedia(DARK_QUERY)
    } catch {
      return undefined
    }

    const onChange = (mediaEvent) => setThemeState(mediaEvent.matches ? DARK : LIGHT)
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [theme])

  const setTheme = useCallback((next) => {
    storeTheme(next)
    setThemeState(next)
  }, [])

  const toggleTheme = useCallback(() => {
    setThemeState((current) => {
      const next = current === DARK ? LIGHT : DARK
      // Written inside the updater so the stored value can never disagree with
      // the state, even if two clicks land in the same batch.
      storeTheme(next)
      return next
    })
  }, [])

  const value = useMemo(
    () => ({
      theme,
      isDark: theme === DARK,
      setTheme,
      toggleTheme,
      chart: CHART_COLORS[theme],
      heatmapRamp: HEATMAP_RAMP[theme],
    }),
    [theme, setTheme, toggleTheme],
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}
