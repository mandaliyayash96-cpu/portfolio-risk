/**
 * The dashboard's sections: which exist, in what order, and what each is for.
 *
 * A plain module rather than part of DashboardTabs.jsx, for the same reason
 * theme-context.js and auth-context.js are split from their components: Fast
 * Refresh can only hot-swap a file whose exports are all components, and
 * `react-refresh/only-export-components` flags a file that exports both.
 *
 * This is the single source for both halves of the feature - the buttons in
 * <DashboardTabs> and the panels in Dashboard.jsx - so adding a section is one
 * entry here plus one <Section> there, and the two cannot disagree about what
 * a tab is called or where it sits.
 */

/**
 * `hint` is the line under the tab bar, and it is not decoration. With only one
 * section on screen the label alone has to carry what you are looking at, and
 * "Rebalance" does not say "Markowitz suggestion".
 */
export const DASHBOARD_TABS = [
  {
    id: 'overview',
    label: 'Overview',
    hint: 'Headline risk metrics and what the portfolio holds',
  },
  {
    id: 'performance',
    label: 'Performance',
    hint: 'Value curve and drawdown over the measured window',
  },
  {
    id: 'risk',
    label: 'Risk',
    hint: 'Value at Risk, and how the holdings move together',
  },
  {
    id: 'rebalance',
    label: 'Rebalance',
    hint: 'Markowitz suggestion against the current weights',
  },
  {
    id: 'holdings',
    label: 'Holdings',
    hint: 'Every position, and the forms that change them',
  },
  {
    id: 'alerts',
    label: 'Alerts',
    hint: 'Rules, and the live feed of what has fired',
  },
]

/** Where the dashboard opens. First tab, and the summary of the rest. */
export const DEFAULT_TAB = DASHBOARD_TABS[0].id
