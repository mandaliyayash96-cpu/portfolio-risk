/**
 * The light/dark switch.
 *
 * Labelled with its DESTINATION, not its current state: in light mode it shows
 * a moon and the word "Dark", because that is what clicking it does. An icon
 * alone is ambiguous - a moon can equally mean "you are in dark mode" - and
 * this gets read from the back of a hall, so the word settles it.
 */

import { useTheme } from '../theme-context'

/* Both icons inherit `currentColor`, so they theme with the button's text and
   need no colour of their own. */

function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false">
      <path
        d="M20 14.2A8.2 8.2 0 0 1 9.8 4a8.4 8.4 0 1 0 10.2 10.2Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="4.2" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <g stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
        <path d="M12 2.6v2.2M12 19.2v2.2M2.6 12h2.2M19.2 12h2.2" />
        <path d="M5.4 5.4 7 7M17 17l1.6 1.6M18.6 5.4 17 7M7 17l-1.6 1.6" />
      </g>
    </svg>
  )
}

export default function ThemeToggle() {
  const { isDark, toggleTheme } = useTheme()
  const target = isDark ? 'light' : 'dark'

  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={toggleTheme}
      // The accessible name has to say what the button DOES; the visible label
      // is only one word and would read as a state to a screen reader.
      aria-label={`Switch to ${target} mode`}
      title={`Switch to ${target} mode`}
    >
      {isDark ? <SunIcon /> : <MoonIcon />}
      <span className="theme-toggle__label">{isDark ? 'Light' : 'Dark'}</span>
    </button>
  )
}
