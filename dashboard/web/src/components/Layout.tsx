/**
 * Root layout: sidebar nav + main content area + a top-bar health badge.
 *
 * Uses React Router's `<Outlet>` to render the active page. The sidebar
 * is sticky on tall viewports and collapses gracefully on narrow ones
 * (operator-targeted tool - no mobile-first design needed, just legible).
 */
import { NavLink, Outlet } from 'react-router-dom'

import { HealthBadge } from './HealthBadge'

const NAV_ITEMS = [
  { to: '/runs', label: 'Runs' },
  { to: '/sites', label: 'Sites' },
  { to: '/reports', label: 'Reports' },
] as const

export function Layout() {
  return (
    <div className="flex h-full bg-slate-50 text-slate-900">
      <aside className="flex w-56 flex-col border-r border-slate-200 bg-white">
        <div className="border-b border-slate-200 px-5 py-4">
          <div className="text-sm font-semibold tracking-wide text-slate-900">
            AFR Dashboard
          </div>
          <div className="text-xs text-slate-500">UI regression runs</div>
        </div>
        <nav className="flex-1 px-2 py-3">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `block rounded-md px-3 py-2 text-sm font-medium transition-colors ${
                  isActive
                    ? 'bg-slate-900 text-white'
                    : 'text-slate-700 hover:bg-slate-100'
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-slate-200 px-3 py-3">
          <HealthBadge />
        </div>
      </aside>
      <main className="flex-1 overflow-auto">
        <Outlet />
      </main>
    </div>
  )
}
