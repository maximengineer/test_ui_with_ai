/**
 * Sites page: full CRUD over `sites.yml` via the dashboard API.
 *
 * Layout:
 *   - Single-add form (always visible) for the common one-URL case.
 *   - Bulk-add panel (collapsed by default) for pasting many URLs.
 *   - Sites table with inline edit + delete.
 *
 * IDs are auto-assigned as sequential numbers (1, 2, 3, ...) by the
 * backend's `next_numeric_id`. Operator never sets an id; rename via
 * delete + re-create.
 */
import { useMemo, useState } from 'react'

import {
  useBulkCreateSites,
  useBulkDeleteSites,
  useCreateSite,
  useDeleteSite,
  useSites,
  useUpdateSite,
  type SiteOut,
} from '@/api/hooks'

export function SitesPage() {
  const sites = useSites()
  const create = useCreateSite()
  const bulk = useBulkCreateSites()
  const bulkDelete = useBulkDeleteSites()
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [editingId, setEditingId] = useState<string | null>(null)
  const [bulkOpen, setBulkOpen] = useState(false)
  const [bulkText, setBulkText] = useState('')
  // Bulk-select state. Mirrors RunsPage: Set<string> for fast contains
  // checks per-row, gets passed through `bulk_delete_sites` as a list.
  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set())

  // Memo'd so the `?? []` fallback doesn't allocate a fresh array each
  // render; otherwise the `allChecked` useMemo below would re-fire on
  // every parent re-render and the eslint exhaustive-deps lint complains.
  const items = useMemo(() => sites.data ?? [], [sites.data])
  // True when EVERY visible row is checked - drives the header checkbox
  // tristate.
  const allChecked = useMemo(
    () => items.length > 0 && items.every((s) => checkedIds.has(s.id)),
    [items, checkedIds],
  )

  function toggleOne(id: string) {
    setCheckedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function toggleAll() {
    setCheckedIds((prev) => {
      const next = new Set(prev)
      if (allChecked) {
        for (const s of items) next.delete(s.id)
      } else {
        for (const s of items) next.add(s.id)
      }
      return next
    })
  }

  function clearSelection() {
    setCheckedIds(new Set())
  }

  function confirmAndBulkDelete() {
    const ids = Array.from(checkedIds)
    if (ids.length === 0) return
    // Show up to 5 ids in the prompt so the operator can sanity-check
    // what's about to disappear; truncate beyond that to keep the
    // dialog readable.
    const preview = ids.slice(0, 5).join(', ')
    const tail = ids.length > 5 ? `, …(+${ids.length - 5} more)` : ''
    if (
      !window.confirm(
        `Remove ${ids.length} site${ids.length === 1 ? '' : 's'}?\n\n` +
          `IDs: ${preview}${tail}\n\n` +
          'On-disk per-site data dirs are NOT touched - historical ' +
          'reports stay readable; only future runs stop including these sites.',
      )
    ) {
      return
    }
    bulkDelete.mutate(ids, {
      onSuccess: (result) => {
        // If the row being edited just got deleted, close the inline form.
        if (editingId !== null && (result.deleted ?? []).includes(editingId)) {
          setEditingId(null)
        }
        clearSelection()
      },
    })
  }

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

  function submitBulk(e: React.FormEvent) {
    e.preventDefault()
    // Split on newlines, trim, drop blanks. The backend rejects empty
    // strings (min_length=1) so the trim+filter on the client is purely
    // a UX nicety - operator pastes a list with stray empty lines and
    // we don't make them clean it manually.
    const urls = bulkText
      .split('\n')
      .map((s) => s.trim())
      .filter((s) => s.length > 0)
    if (urls.length === 0) return
    bulk.mutate(urls, {
      onSuccess: () => {
        setBulkText('')
        setBulkOpen(false)
      },
    })
  }

  const parsedBulkCount = bulkText
    .split('\n')
    .map((s) => s.trim())
    .filter((s) => s.length > 0).length

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
          <button
            type="button"
            onClick={() => setBulkOpen((v) => !v)}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-100"
          >
            {bulkOpen ? 'Hide bulk' : 'Add multiple'}
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

        {bulkOpen && (
          <form
            onSubmit={submitBulk}
            className="mt-3 rounded-md border border-slate-200 bg-slate-50 p-3"
          >
            <label className="block text-xs text-slate-600">
              <span className="mb-1 block font-medium">
                Paste URLs (one per line)
              </span>
              <textarea
                value={bulkText}
                onChange={(e) => setBulkText(e.target.value)}
                rows={6}
                placeholder={
                  'https://example.com/foo\nhttps://example.com/bar\nhttps://example.com/baz'
                }
                className="w-full rounded-md border border-slate-300 bg-white px-2 py-1 font-mono text-xs"
              />
            </label>
            <div className="mt-2 flex items-center justify-between">
              <span className="text-xs text-slate-500">
                {parsedBulkCount === 0
                  ? 'IDs and names auto-generated. URL becomes the name (rename later via Edit).'
                  : `${parsedBulkCount} URL${parsedBulkCount === 1 ? '' : 's'} ready to add`}
              </span>
              <button
                type="submit"
                disabled={bulk.isPending || parsedBulkCount === 0}
                className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-50 hover:bg-slate-700"
              >
                {bulk.isPending
                  ? 'Adding…'
                  : `Add ${parsedBulkCount || ''} site${parsedBulkCount === 1 ? '' : 's'}`}
              </button>
            </div>
            {bulk.isError && (
              <div className="mt-2 flex items-start justify-between gap-2 text-xs text-red-700">
                <span>
                  Bulk add failed (no sites were added):{' '}
                  {(bulk.error as Error).message}
                </span>
                <button
                  type="button"
                  onClick={() => bulk.reset()}
                  className="rounded px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
                  aria-label="Dismiss error"
                >
                  Dismiss
                </button>
              </div>
            )}
          </form>
        )}
      </div>

      {/* Bulk action bar - only visible when at least one row is checked.
          Mirrors the RunsPage pattern so the affordance reads identically
          across the dashboard. */}
      {checkedIds.size > 0 && (
        <div className="flex items-center justify-between border-b border-slate-200 bg-blue-50 px-6 py-2 text-sm">
          <span className="text-slate-700">
            {checkedIds.size} site{checkedIds.size === 1 ? '' : 's'} selected
          </span>
          <div className="flex gap-2">
            <button
              onClick={clearSelection}
              className="rounded border border-slate-300 bg-white px-3 py-1 text-xs hover:bg-slate-100"
            >
              Clear
            </button>
            <button
              onClick={confirmAndBulkDelete}
              disabled={bulkDelete.isPending}
              className="rounded bg-red-600 px-3 py-1 text-xs font-medium text-white disabled:opacity-50 hover:bg-red-700"
            >
              {bulkDelete.isPending ? 'Deleting…' : `Delete ${checkedIds.size}`}
            </button>
          </div>
        </div>
      )}

      {/* Bulk-delete outcome - one-cycle toast after a successful mutation.
          Skipped lists matter when the operator selected an id that another
          tab already deleted; this surfaces it cleanly. */}
      {bulkDelete.isSuccess && bulkDelete.data && (
        <BulkDeleteResultBanner
          result={bulkDelete.data}
          onDismiss={() => bulkDelete.reset()}
        />
      )}
      {bulkDelete.isError && (
        <div className="flex items-center justify-between border-b border-red-200 bg-red-50 px-6 py-2 text-sm text-red-800">
          <span>
            Bulk delete failed: {(bulkDelete.error as Error).message}
          </span>
          <button
            onClick={() => bulkDelete.reset()}
            className="rounded px-2 py-1 text-xs hover:bg-red-100"
          >
            Dismiss
          </button>
        </div>
      )}

      <div className="flex-1 overflow-auto">
        {sites.isError ? (
          <div className="p-6 text-sm text-red-700">
            Failed to load sites: {(sites.error as Error).message}
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="sticky top-0 border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="w-[1%] px-4 py-2 text-left">
                  <input
                    type="checkbox"
                    checked={allChecked}
                    onChange={toggleAll}
                    aria-label="Select all sites"
                    className="cursor-pointer"
                  />
                </th>
                <th className="px-4 py-2 text-left">ID</th>
                <th className="px-4 py-2 text-left">Name</th>
                <th className="px-4 py-2 text-left">URL</th>
                <th className="w-[1%] whitespace-nowrap px-4 py-2 text-right">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((site) => (
                <SiteRow
                  // Key strategy:
                  //   - When NOT editing, include name+url so a remote
                  //     update (different tab / poll) remounts the row
                  //     and refreshes the inline-edit form's initial
                  //     state from new props. Round-2 #M9 fix.
                  //   - When editing THIS row, drop name+url from the
                  //     key so an in-flight remote update doesn't wipe
                  //     the user's mid-typing input. Round-3 #M1 fix.
                  key={
                    editingId === site.id
                      ? site.id
                      : `${site.id}::${site.name}::${site.url}`
                  }
                  site={site}
                  editing={editingId === site.id}
                  checked={checkedIds.has(site.id)}
                  onToggle={() => toggleOne(site.id)}
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
// SiteRow - renders either a static row or an inline edit form.
// --------------------------------------------------------------------------

function SiteRow({
  site,
  editing,
  checked,
  onToggle,
  onStartEdit,
  onCancelEdit,
  onSaveDone,
}: {
  site: SiteOut
  editing: boolean
  checked: boolean
  onToggle: () => void
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
    update.mutate({ id: site.id, name, url }, { onSuccess: onSaveDone })
  }

  function confirmDelete() {
    if (!window.confirm(`Remove site "${site.name}" (${site.id})?`)) return
    remove.mutate(site.id)
  }

  // Per-row checkbox cell. Identical structure in both edit + view modes
  // so the column alignment stays clean and the operator can select rows
  // without leaving edit mode.
  const checkboxCell = (
    <td className="w-[1%] px-4 py-2">
      <input
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        aria-label={`Select site ${site.id}`}
        className="cursor-pointer"
      />
    </td>
  )

  if (editing) {
    return (
      <tr className="border-b border-slate-100 bg-blue-50">
        {checkboxCell}
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
        <td className="w-[1%] whitespace-nowrap px-4 py-2 text-right">
          <form
            onSubmit={submitEdit}
            className="flex justify-end gap-2"
          >
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
      {checkboxCell}
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
      {/*
        Action buttons: `flex` + `gap-2` keeps Edit and Delete on one row.
        `whitespace-nowrap` + `w-[1%]` on the cell forces the column to
        shrink to fit (so the URL column gets the leftover width) and
        the buttons never wrap onto two lines. Pre-fix the column was
        narrow + buttons stacked vertically (operator screenshot).
      */}
      <td className="w-[1%] whitespace-nowrap px-4 py-2 text-right">
        <div className="flex justify-end gap-2">
          <button
            onClick={onStartEdit}
            className="rounded border border-slate-300 px-2 py-1 text-xs hover:bg-slate-100"
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
        </div>
      </td>
    </tr>
  )
}

// --------------------------------------------------------------------------
// BulkDeleteResultBanner - toast shown for one cycle after a successful
// bulk-delete mutation. Same color vocabulary as the RunsPage banner:
// green when fully clean, amber when anything was skipped (operator
// should notice the no-op).
// --------------------------------------------------------------------------

function BulkDeleteResultBanner({
  result,
  onDismiss,
}: {
  result: {
    deleted?: string[]
    skipped_not_found?: string[]
  }
  onDismiss: () => void
}) {
  // OpenAPI marks these optional (Pydantic default_factory) - normalize
  // to empty arrays for arithmetic.
  const deleted = result.deleted ?? []
  const skipped = result.skipped_not_found ?? []
  const message = skipped.length
    ? `Deleted ${deleted.length}. Skipped ${skipped.length} (already gone: ${skipped.join(', ')}).`
    : `Deleted ${deleted.length} site${deleted.length === 1 ? '' : 's'}.`
  const tone = skipped.length
    ? 'border-amber-200 bg-amber-50 text-amber-900'
    : 'border-green-200 bg-green-50 text-green-900'
  return (
    <div
      className={`flex items-center justify-between border-b ${tone} px-6 py-2 text-sm`}
    >
      <span>{message}</span>
      <button
        onClick={onDismiss}
        className="rounded px-2 py-1 text-xs hover:bg-white"
      >
        Dismiss
      </button>
    </div>
  )
}
