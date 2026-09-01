/**
 * The app's mark: a rising line inside a filled square.
 *
 * Its own component because it appears on both sides of the auth boundary - the
 * sign-in card and the dashboard header - and those are the two screens that
 * most need to look like the same product. A copy in each would be two things
 * to change and one to forget.
 *
 * Inline SVG rather than an image file: it is nine path commands, it inherits
 * `currentColor` so it themes with the square it sits in, and it costs no
 * request on a first paint.
 */

export default function BrandMark({ className = '' }) {
  return (
    <span className={`brandmark ${className}`.trim()} aria-hidden="true">
      <svg viewBox="0 0 24 24" focusable="false">
        <path
          d="M4 16.5 9.5 11l3.5 3.5L20 7.5"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <path
          d="M15.5 7.5H20v4.5"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  )
}
