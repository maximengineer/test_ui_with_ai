/**
 * Typed dashboard API client.
 *
 * `openapi-fetch` wraps the generated `paths` interface so every call gets
 * full request/response typing for free. Vite's dev-server proxy forwards
 * `/api/*` to the FastAPI backend on :8080 (see vite.config.ts), so the
 * baseUrl is empty here - the browser sees a same-origin call.
 *
 * Regenerate `schema.gen.ts` with `npm run gen:api` whenever the backend
 * adds / changes a route. The script reads the snapshot at
 * `schemas/dashboard-openapi.json`; that file is committed to git so the
 * frontend can rebuild against a known-good backend version even if the
 * backend isn't running.
 */
import createClient from 'openapi-fetch'

import type { paths } from './schema.gen'

export const api = createClient<paths>({
  // Same-origin in both dev (via Vite proxy) and production (FastAPI
  // serves the SPA same-origin per Phase C.3).
  baseUrl: '',
})
