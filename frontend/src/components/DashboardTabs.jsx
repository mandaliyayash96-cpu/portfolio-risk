/**
 * The dashboard's top navigation: one section on screen at a time.
 *
 * A real ARIA tablist rather than a row of styled buttons, because that is what
 * this is and the difference is free: `role="tablist"` plus roving focus means
 * a keyboard user moves between sections with the arrow keys and hears "tab 3
 * of 6, selected", instead of tabbing through six unlabelled buttons.
 *
 * ROVING TABINDEX
 * ---------------
 * Only the SELECTED tab is in the tab order (`tabIndex 0`); the rest are -1.
 * That is the standard pattern and the reason for the key handler below: Tab
 * moves you out of the tablist and into the panel, arrows move you within it.
 * Without it, reaching the page content past six tabs takes six keystrokes.
 *
 * The component holds no state. Which tab is active is the dashboard's
 * business - it is the thing that knows what a tab MEANS - so this renders
 * what it is told and reports clicks upward.
 */

import { useRef } from 'react'

import { DASHBOARD_TABS } from './dashboard-tabs'

export default function DashboardTabs({ active, onChange }) {
  const buttonsRef = useRef([])

  /**
   * Arrow keys move within the tablist; Home/End jump to the ends.
   *
   * The move both SELECTS and FOCUSES, which is the "automatic activation"
   * flavour of the pattern - right for tabs like these, where switching is
   * instant and costs nothing. (Manual activation, where arrows only move
   * focus and Enter selects, is for tabs whose panels are expensive to build.)
   */
  function onKeyDown(event) {
    const index = DASHBOARD_TABS.findIndex((tab) => tab.id === active)
    if (index < 0) return

    let next = null
    if (event.key === 'ArrowRight') next = (index + 1) % DASHBOARD_TABS.length
    else if (event.key === 'ArrowLeft') next = (index - 1 + DASHBOARD_TABS.length) % DASHBOARD_TABS.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = DASHBOARD_TABS.length - 1
    if (next === null) return

    event.preventDefault()
    onChange(DASHBOARD_TABS[next].id)
    buttonsRef.current[next]?.focus()
  }

  const current = DASHBOARD_TABS.find((tab) => tab.id === active) ?? DASHBOARD_TABS[0]

  return (
    <nav className="tabs" aria-label="Dashboard sections">
      <div className="tabs__list" role="tablist" onKeyDown={onKeyDown}>
        {DASHBOARD_TABS.map((tab, index) => {
          const isActive = tab.id === active
          return (
            <button
              key={tab.id}
              ref={(node) => {
                buttonsRef.current[index] = node
              }}
              type="button"
              role="tab"
              id={`tab-${tab.id}`}
              aria-selected={isActive}
              aria-controls={`panel-${tab.id}`}
              tabIndex={isActive ? 0 : -1}
              className={`tab ${isActive ? 'tab--active' : ''}`}
              onClick={() => onChange(tab.id)}
            >
              {tab.label}
            </button>
          )
        })}
      </div>

      <p className="tabs__hint">{current.hint}</p>
    </nav>
  )
}
