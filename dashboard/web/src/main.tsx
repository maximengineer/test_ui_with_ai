/**
 * SPA entry point. Wires the three pieces every page needs:
 *   1. TanStack Query for server state (caching + polling)
 *   2. React Router for client-side navigation
 *   3. The root layout (sidebar + outlet)
 */
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createBrowserRouter, Navigate, RouterProvider } from 'react-router-dom'

import { Layout } from '@/components/Layout'
import { RunsPage } from '@/pages/RunsPage'
import { SitesPage } from '@/pages/SitesPage'
import { ReportsPage } from '@/pages/ReportsPage'

import './index.css'

// Single shared QueryClient. Defaults are fine for the dashboard's small
// surface area; per-hook `refetchInterval` opts into polling where it
// makes sense (health, runs list, run detail, logs).
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Refetch on focus IS desirable here: subprocesses are progressing
      // server-side while the operator's tab may be backgrounded, and
      // TanStack Query's default `refetchIntervalInBackground: false`
      // means our 3s/2s polling intervals pause whenever the tab is
      // hidden. Without focus refetch, returning to the tab leaves
      // stale pills (baseline shows "running" when it's actually "done")
      // until the next interval tick - which is what manifested as
      // "I have to refresh the page to see status updates". Focus
      // refetch fires immediately on tab-back, then the per-hook
      // `refetchInterval` resumes normal polling.
      refetchOnWindowFocus: true,
      // One retry on transient failures; more would spam the backend.
      retry: 1,
    },
  },
})

const router = createBrowserRouter([
  {
    path: '/',
    element: <Layout />,
    children: [
      // Default landing: Sites - matches the operator workflow
      // (configure sites first, then trigger runs, then read reports).
      { index: true, element: <Navigate to="/sites" replace /> },
      { path: 'sites', element: <SitesPage /> },
      { path: 'runs', element: <RunsPage /> },
      { path: 'reports', element: <ReportsPage /> },
    ],
  },
])

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
)
