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
      // Refetch on focus is too aggressive for a dashboard the operator
      // tabs back to repeatedly - we rely on the per-hook interval.
      refetchOnWindowFocus: false,
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
      // Default landing: Runs (most actionable for the operator).
      { index: true, element: <Navigate to="/runs" replace /> },
      { path: 'runs', element: <RunsPage /> },
      { path: 'sites', element: <SitesPage /> },
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
