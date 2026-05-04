import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

// Vite config for the dashboard SPA.
//
// `proxy: '/api'` forwards XHRs to the FastAPI backend during dev so the
// SPA and API share an origin from the browser's POV - same posture as
// production where FastAPI serves the SPA same-origin (Phase C.3 work).
// Without this we'd be relying on the dev-mode CORS middleware for every
// request, which works but masks any same-origin assumptions in the code.
//
// The `@/` import alias keeps deep-relative paths from creeping in as
// the `src/` tree grows (`../../components/foo` is unreviewable).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8080',
        changeOrigin: true,
      },
    },
  },
})
