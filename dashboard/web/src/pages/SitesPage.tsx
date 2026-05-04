/**
 * Sites page: full CRUD over `sites.yml` via the dashboard API.
 *
 * Layout: a top "add" form (always visible), then the table. Each row
 * has inline edit (toggles to a 2-input form) + delete (with a JS
 * confirm — keeping the MVP simple; a confirm modal would be easy to
 * add later if the operator-friction outweighs the click).
 *
 * The id is server-generated from the slugified name, so the create form
 * has no id field. Edits are name + URL only (id is immutable; rename
 * via delete + recreate).
 */
import { useState } from 'react'

import {
  useCreateSite,
  useDeleteSite,
  useSites,
  useUpdateSite,
  type SiteOut,
} from '@/api/hooks'

export function SitesPage() {
  const sites = useSites()
  const create = useCreateSite()
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [editingId, setEditingId] = useState<string | null>(null)

  function submitCreate(e: React.FormEvent) {
    e.preventDefault()
    create.mutate(
      { name: name.trim(), url: url.trim() },
      {
        onSuccess: () => {
          setName('')
          setUrl('')
        },
      },
    )
  }

  return (
    <div className="flex h-full flex-col">
      <header className="border-b border-slate-200 bg-white px-6 py-4">
        <h1 className="text-xl font-semibold">Sites</h1>
        <p className="text-sm text-slate-500">
          {sites.isLoading
            ? 'Loading…'
            : `${sites.data?.length ?? 0} configured`}
        </p>
      </header>

      <div className="border-b border-slate-200 bg-white px-6 py-3">
        <form onSubmit={submitCreate} className="flex items-end gap-2">
          <label className="flex flex-col text-xs text-slate-600">
            <span className="mb-1">Name</span>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              minLength={1}
              maxLength={200}
              placeholder="e.g. Department of Health"
              className="rounded-md border border-slate-300 px-2 py-1 text-sm"
            />
          </label>
          <label className="flex flex-1 flex-col text-xs text-slate-600">
            <span className="mb-1">URL</span>
            <input
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              required
              maxLength={2048}
              placeholder="https://example.com/path"
              className="rounded-md border border-slate-300 px-2 py-1 text-sm"
            />
          </label>
          <button
            type="submit"
            disabled={create.isPending || !name.trim() || !url.trim()}
            className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-50 hover:bg-slate-700"
          >
            {create.isPending ? 'Adding…' : 'Add site'}
          </button>
        </form>
        {create.isError && (
          <div className="mt-2 flex items-start justify-between gap-2 text-xs text-red-700">
            <span>{(create.error as Error).message}</span>
            <button
              type="button"
              onClick={() => create.reset()}
              className="rounded px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
              aria-label="Dismiss error"
            >
              Dismiss
            </button>
          </div>
        )}
      </div>

      <div className="flex-1 overflow-auto">
        {sites.isError ? (
          <div className="p-6 text-sm text-red-700">
            Failed to load sites: {(sites.error as Error).message}
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="sticky top-0 border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-2 text-left">ID</th>
                <th className="px-4 py-2 text-left">Name</th>
                <th className="px-4 py-2 text-left">URL</th>
                <th className="px-4 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(sites.data ?? []).map((site) => (
                <SiteRow
                  // Key strategy:
                  //   - When NOT editing, include name+url so a remote
                  //     update (different tab / poll) remounts the row
                  //     and refreshes the inline-edit form's initial
                  //     state from new props. Round-2 #M9 fix.
                  //   - When editing THIS row, drop name+url from the
                  //     key so an in-flight remote update doesn't wipe
                  //     the user's mid-typing input. Round-3 #M1 fix —
                  //     the M9 fix was correct for stale-edit prevention
                  //     but introduced a worse bug where the user's
                  //     own typing could vanish if a refetch landed
                  //     between keystrokes.
                  key={
                    editingId === site.id
                      ? site.id
                      : `${site.id}::${site.name}::${site.url}`
                  }
                  site={site}
                  editing={editingId === site.id}
                  onStartEdit={() => setEditingId(site.id)}
                  onCancelEdit={() => setEditingId(null)}
                  onSaveDone={() => setEditingId(null)}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

// --------------------------------------------------------------------------
// SiteRow — renders either a static row or an inline edit form.
// --------------------------------------------------------------------------

function SiteRow({
  site,
  editing,
  onStartEdit,
  onCancelEdit,
  onSaveDone,
}: {
  site: SiteOut
  editing: boolean
  onStartEdit: () => void
  onCancelEdit: () => void
  onSaveDone: () => void
}) {
  const update = useUpdateSite()
  const remove = useDeleteSite()
  const [name, setName] = useState(site.name)
  const [url, setUrl] = useState(site.url)

  function submitEdit(e: React.FormEvent) {
    e.preventDefault()
    update.mutate(
      { id: site.id, name, url },
      { onSuccess: onSaveDone },
    )
  }

  function confirmDelete() {
    if (!window.confirm(`Remove site "${site.name}" (${site.id})?`)) return
    remove.mutate(site.id)
  }

  if (editing) {
    return (
      <tr className="border-b border-slate-100 bg-blue-50">
        <td className="px-4 py-2 font-mono text-xs text-slate-500">{site.id}</td>
        <td className="px-4 py-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </td>
        <td className="px-4 py-2">
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </td>
        <td className="px-4 py-2 text-right">
          <form onSubmit={submitEdit} className="inline-flex gap-2">
            <button
              type="submit"
              disabled={update.isPending}
              className="rounded bg-slate-900 px-2 py-1 text-xs font-medium text-white disabled:opacity-50 hover:bg-slate-700"
            >
              Save
            </button>
            <button
              type="button"
              onClick={onCancelEdit}
              className="rounded border border-slate-300 px-2 py-1 text-xs hover:bg-slate-100"
            >
              Cancel
            </button>
          </form>
        </td>
      </tr>
    )
  }

  return (
    <tr className="border-b border-slate-100 hover:bg-slate-50">
      <td className="px-4 py-2 font-mono text-xs text-slate-500">{site.id}</td>
      <td className="px-4 py-2">{site.name}</td>
      <td className="px-4 py-2 text-xs text-slate-600">
        <a
          href={site.url}
          target="_blank"
          rel="noreferrer noopener"
          className="hover:underline"
        >
          {site.url}
        </a>
      </td>
      <td className="px-4 py-2 text-right">
        <button
          onClick={onStartEdit}
          className="mr-2 rounded border border-slate-300 px-2 py-1 text-xs hover:bg-slate-100"
        >
          Edit
        </button>
        <button
          onClick={confirmDelete}
          disabled={remove.isPending}
          className="rounded border border-red-300 px-2 py-1 text-xs text-red-700 disabled:opacity-50 hover:bg-red-50"
        >
          {remove.isPending ? '…' : 'Delete'}
        </button>
      </td>
    </tr>
  )
}
