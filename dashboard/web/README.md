# Dashboard Web

React + TypeScript SPA for the local AI UI regression dashboard.

The app is served in two modes:

- Development: Vite dev server on `http://localhost:5173`, proxying `/api/*` to the FastAPI backend on `:8080`.
- Production/Docker: built static bundle served same-origin by FastAPI from `dashboard/api/spa.py`.

## Commands

Run from `dashboard/web/` unless noted otherwise.

```bash
npm install
npm run dev          # Vite only
npm run dev:full     # Vite + uvicorn backend via concurrently
npm run build        # TypeScript check + production bundle
npm run lint
npm run gen:api      # regenerate src/api/schema.gen.ts from schemas/dashboard-openapi.json
```

From the repository root, the common targets are:

```bash
make dashboard-dev
make dashboard-docker
make dashboard-build
```

## API Contract

The frontend consumes generated OpenAPI types:

- Source snapshot: `../../schemas/dashboard-openapi.json`
- Generated TypeScript: `src/api/schema.gen.ts`
- Typed client wrapper: `src/api/client.ts`
- Domain hooks: `src/api/hooks/`

If backend routes or Pydantic response models change, regenerate the OpenAPI snapshot and frontend types in the same change:

```bash
python -c "import json; from dashboard.api.main import create_app; print(json.dumps(create_app(dev_mode=False).openapi(), indent=2))" > schemas/dashboard-openapi.json
cd dashboard/web && npm run gen:api
```

## Structure

```text
src/
├── api/          # generated schema, typed client, TanStack Query hooks
├── components/   # shared UI components
├── lib/          # dashboard-specific helpers such as session grouping
├── pages/        # route-level pages
└── main.tsx      # React entrypoint
```

Keep page files thin when adding behavior. Prefer small action/state/view modules, especially for Runs, Reports, and Sites pages.
