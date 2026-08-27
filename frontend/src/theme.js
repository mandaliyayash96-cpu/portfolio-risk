/**
 * Theme state, and the colours that CSS cannot reach.
 *
 * The palette proper lives in index.css as custom properties on `:root` and
 * `[data-theme="dark"]`. Everything the browser paints - panels, tables, text,
 * borders - themes itself from there the moment `data-theme` flips, with no
 * JavaScript involved and no re-render.
 *
 * This module exists for the two things that arrangement cannot cover:
 *
 *   1. Deciding WHICH theme is active (stored choice, else the OS preference)
 *      and remembering the answer.
 *   2. Chart series colours. Recharts draws SVG, and while `var()` does resolve
 *      in SVG presentation attributes in modern browsers - which is why the
 *      existing axes already themed - series colours cannot be handled that way
 *      for two reasons. A hue chosen to read on white is not the same hue that
 *      reads on near-black (#1d4ed8 is a confident blue on paper and a muddy
 *      smudge on a dark panel, so this is a different value, not the same value
 *      re-expressed), and Recharts' <Legend> renders its labels through inline
 *      styles derived from the series colour in JS, where `var()` is just an
 *      unresolvable string.
 *
 * CHART_COLORS therefore duplicates a handful of values that also appear in
 * index.css. That duplication is deliberate and small; the pairs are noted
 * inline so a change to one is a visible prompt to check the other.
 */

export const LIGHT = 'light'
export const DARK = 'dark'

/** Namespaced so it cannot collide with anything else on localhost. */
export const STORAGE_KEY = 'portfolio-risk:theme'

/** The media query that reports the OS-level preference. */
export const DARK_QUERY = '(prefers-color-scheme: dark)'

const isTheme = (value) => value === LIGHT || value === DARK

/**
 * The theme the user explicitly chose, or null if they never have.
 *
 * Wrapped because localStorage is not always available: Safari private mode
 * historically threw on write, and a browser set to block all site data throws
 * on read. A dashboard must not fail to render over a colour preference.
 */
export function readStoredTheme() {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY)
    return isTheme(stored) ? stored : null
  } catch {
    return null
  }
}

/** Remember an explicit choice. Silently a no-op if storage is unavailable. */
export function storeTheme(theme) {
  try {
    window.localStorage.setItem(STORAGE_KEY, theme)
  } catch {
    // Ignored on purpose - the toggle still works for this session.
  }
}

/** What the operating system is currently asking for. */
export function systemTheme() {
  try {
    return window.matchMedia(DARK_QUERY).matches ? DARK : LIGHT
  } catch {
    return LIGHT
  }
}

/**
 * The theme to start in: an explicit choice if there is one, else the OS.
 *
 * Defaulting to the system preference rather than hard-coding light is the
 * deliberate call. A dashboard opened on a machine already in dark mode should
 * not flash a white page at a darkened room; and anyone who wants the other one
 * is one click away, after which their choice wins permanently.
 */
export function resolveInitialTheme() {
  return readStoredTheme() ?? systemTheme()
}

/**
 * Colours for the Recharts SVG, per theme.
 *
 * The values marked "mirrors --x" are the same colour as that CSS custom
 * property in index.css. Keep them in step.
 */
export const CHART_COLORS = {
  [LIGHT]: {
    axis: '#5c6b81', //        mirrors --ink-muted
    axisLine: '#d8dfe9', //    mirrors --border
    grid: '#e8edf4', //        mirrors --border-soft
    cursor: '#f1f5f9', //      mirrors --surface-sunken
    // Slices and markers are outlined in the panel colour so they read as cut
    // out of the card rather than stacked on top of it.
    surface: '#ffffff', //     mirrors --surface

    /**
     * Categorical ramp for the allocation pie: eight hues that stay
     * distinguishable on a washed-out projector and in greyscale print.
     * Deliberately not the red/green risk vocabulary - a slice of an allocation
     * is neither good nor bad.
     */
    categorical: [
      '#2563eb',
      '#0891b2',
      '#7c3aed',
      '#c2410c',
      '#0f766e',
      '#a21caf',
      '#4d7c0f',
      '#b45309',
    ],

    /**
     * The Performance panel's two charts.
     *
     * `value` is --accent and `drawdown` is --bad, reused rather than invented:
     * the underwater chart IS a risk chart, so it belongs in this dashboard's
     * red/green risk vocabulary, and the value curve is the page's primary
     * series exactly as the accent is its primary colour.
     *
     * `label` is body ink, for the callout on the deepest drawdown - a data
     * label wears a text colour, never the series colour.
     */
    performance: {
      value: '#1d4ed8', //     mirrors --accent
      drawdown: '#b91c1c', //  mirrors --bad
      label: '#0f172a', //     mirrors --ink
    },

    /** The three VaR estimates. Ordered as they appear on the chart. */
    var: {
      historical: '#1d4ed8',
      parametric: '#0891b2',
      montecarlo: '#7c3aed',
    },

    /** The efficient frontier and the three portfolios marked on it. */
    frontier: {
      // Darkened from the slate-400 this used to be: at 2.56:1 on white the
      // curve fell under the 3:1 floor for a non-text graphic and was the first
      // thing to disappear on a projector.
      curve: '#7d8da3',
      current: '#b91c1c', //     mirrors --bad
      minVariance: '#047857', // mirrors --good
      maxSharpe: '#7c3aed',
    },
  },

  [DARK]: {
    axis: '#94a3b8', //        mirrors --ink-muted (dark)
    axisLine: '#2c3a52', //    mirrors --border (dark)
    grid: '#22304a', //        mirrors --border-soft (dark)
    cursor: '#1a2740', //      mirrors --surface-sunken (dark)
    surface: '#111c2e', //     mirrors --surface (dark)

    // Every hue is lifted into the 60-75% lightness band. On a near-black panel
    // a mid-tone loses its identity long before it loses its contrast, so these
    // are brighter rather than merely lighter versions of the light ramp.
    categorical: [
      '#60a5fa',
      '#22d3ee',
      '#a78bfa',
      '#fb923c',
      '#2dd4bf',
      '#e879f9',
      '#a3e635',
      '#fbbf24',
    ],

    performance: {
      value: '#60a5fa', //     mirrors --accent (dark)
      drawdown: '#f87171', //  mirrors --bad (dark)
      label: '#f1f5f9', //     mirrors --ink (dark)
    },

    var: {
      historical: '#60a5fa',
      parametric: '#22d3ee',
      montecarlo: '#a78bfa',
    },

    frontier: {
      // The curve stays deliberately desaturated: it is context, and the three
      // marked portfolios are the subject. Slate-400 clears the panel
      // comfortably without competing with them.
      curve: '#94a3b8',
      current: '#f87171', //     mirrors --bad (dark)
      minVariance: '#34d399', // mirrors --good (dark)
      maxSharpe: '#a78bfa',
    },
  },
}

/**
 * Correlation cell colouring, which is a ramp rather than a fixed palette.
 *
 * Hue runs green (155) to red (2) and saturation rises with |r|, so a pair that
 * barely correlates is nearly invisible and the extremes are unmistakable. Only
 * the LIGHTNESS band differs by theme, and it has to: the light ramp sits at
 * 63-96% so dark text reads on it, which in dark mode would turn the matrix
 * into the one glowing white box on the page. The dark ramp inverts that -
 * 18-42% - so a weak correlation melts into the panel exactly as it melts into
 * the page on white.
 *
 * `base` is where |r| = 0 and `span` how far the lightness travels by |r| = 1.
 * The direction is inferred from `base`: the light ramp darkens as it
 * saturates, the dark ramp brightens.
 *
 * The dark band is NARROWER than a straight mirror of the light one, and the
 * reason is the hue sweep. Around r = 0.6 the ramp passes through yellow, which
 * is intrinsically luminous: at 32% lightness that cell lands squarely in the
 * middle of the luminance range, where neither black nor white text reaches
 * 4.5:1 against it. Holding the dark band at 10-28% keeps every hue - yellow
 * included - dark enough for light text, and the worst cell in either theme
 * then measures 4.93:1. Saturation still runs the full 20-75%, so the ramp
 * loses none of its ability to separate a weak pair from a strong one.
 *
 * Text colour is NOT set here - CorrelationHeatmap picks it per cell from the
 * cell's actual luminance. See the note there for why a lightness threshold
 * cannot do that job.
 */
export const HEATMAP_RAMP = {
  [LIGHT]: { base: 96, span: 33 },
  [DARK]: { base: 10, span: 18 },
}
