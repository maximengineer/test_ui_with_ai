/**
 * Reports page: pick date + run → see URL list with severity → drill into a
 * single URL to see baseline/current/diff screenshots side-by-side and
 * the AI analysis JSON.
 *
 * Layout: 3-column when a URL is selected (date+run picker | URL list |
 * detail panel). Without a selection: 2 columns. Without a run picked:
 * just the date+run picker on the left.
 */
import { useMemo, useState } from 'react'

import {
  reportScreenshotUrl,
  useDates,
  useReportRuns,
  useReportSummary,
  useReportUrlDetail,
  useReportUrls,
  type ReportResultType,
  type ReportUrlSummary,
} from '@/api/hooks'

const RESULT_TYPE_STYLES: Record<ReportResultType, string> = {
  analysis_success: 'bg-green-100 text-green-800',
  analysis_error: 'bg-red-100 text-red-800',
  no_changes: 'bg-slate-100 text-slate-700',
  ai_disabled: 'bg-amber-100 text-amber-800',
  unknown: 'bg-purple-100 text-purple-800',
}

const SEVERITY_STYLES: Record<string, string> = {
  CRITICAL: 'bg-red-600 text-white',
  WARNING: 'bg-amber-500 text-white',
  SAFE: 'bg-green-600 text-white',
}

export function ReportsPage() {
  const dates = useDates()
  const reportDates = useMemo(() => dates.data?.report ?? [], [dates.data?.report])
  const [date, setDate] = useState<string | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const [selectedUrlId, setSelectedUrlId] = useState<string | null>(null)

  // Keep explicit selection when valid; otherwise default to newest date.
  const selectedDate = useMemo(() => {
    if (date && reportDates.includes(date)) return date
    return reportDates[0] ?? null
  }, [date, reportDates])

  const runs = useReportRuns(selectedDate)

  // Keep explicit run selection when valid; otherwise default to newest run.
  const selectedRunId = useMemo(() => {
    const items = runs.data ?? []
    if (runId && items.some((r) => r.run_id === runId)) return runId
    return items[0]?.run_id ?? null
  }, [runId, runs.data])

  const summary = useReportSummary(selectedDate, selectedRunId)
  const urls = useReportUrls(selectedDate, selectedRunId)

  // Avoid stale detail selection when switching date/run or after refresh.
  const activeUrlId = useMemo(() => {
    if (!selectedUrlId) return null
    const items = urls.data?.items ?? []
    return items.some((item) => item.url_id === selectedUrlId)
      ? selectedUrlId
      : null
  }, [selectedUrlId, urls.data])

  return (
    <div className="flex h-full">
      {/* --- Date + run picker (always visible) -------------------------
          Narrow on purpose - the column only ever shows a DD-MM-YYYY
          date and a truncated 12-char run id. Anything wider just steals
          width from the detail panel where the screenshots live. */}
      <div className="flex w-48 flex-col border-r border-slate-200 bg-white">
        <header className="border-b border-slate-200 px-4 py-3">
          <h1 className="text-lg font-semibold">Reports</h1>
          <p className="text-xs text-slate-500">
            {reportDates.length} date{reportDates.length === 1 ? '' : 's'}
          </p>
        </header>
        <div className="flex-1 overflow-auto px-2 py-2">
          {dates.isLoading ? (
            <div className="px-2 py-1 text-xs text-slate-500">Loading…</div>
          ) : reportDates.length === 0 ? (
            <div className="px-2 py-1 text-xs text-slate-500">
              No reports yet. Run a baseline → current → comparator → report
              cycle.
            </div>
          ) : (
            <ul className="space-y-1">
              {reportDates.map((d) => (
                <li key={d}>
                  <button
                    onClick={() => {
                      setDate(d)
                      setRunId(null)
                      setSelectedUrlId(null)
                    }}
                    className={`w-full rounded px-2 py-1 text-left font-mono text-xs ${
                      selectedDate === d
                        ? 'bg-slate-900 text-white'
                        : 'text-slate-700 hover:bg-slate-100'
                    }`}
                  >
                    {d}
                  </button>
                  {selectedDate === d && runs.data && runs.data.length > 0 && (
                    <ul className="ml-2 mt-1 space-y-0.5 border-l border-slate-200 pl-2">
                      {runs.data.map((r) => (
                        <li key={r.run_id}>
                          <button
                            onClick={() => {
                              setRunId(r.run_id)
                              setSelectedUrlId(null)
                            }}
                            className={`w-full truncate rounded px-2 py-1 text-left font-mono text-[10px] ${
                              selectedRunId === r.run_id
                                ? 'bg-slate-200 text-slate-900'
                                : 'text-slate-600 hover:bg-slate-50'
                            }`}
                            title={r.run_id}
                          >
                            {r.run_id.slice(0, 12)}…
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {/* --- URL list ----------------------------------------------------
          Each row is a short numeric URL id + a small status pill, so a
          tight column reads cleanly. Long site URLs already truncate
          with ellipsis in the secondary line. */}
      {selectedDate && selectedRunId && (
        <div className="flex w-64 flex-col border-r border-slate-200 bg-white">
          <header className="border-b border-slate-200 px-4 py-3">
            <div className="text-xs uppercase tracking-wide text-slate-500">
              Run
            </div>
            <div className="font-mono text-xs">{selectedRunId}</div>
            {summary.data && (
              <div className="mt-2 flex flex-wrap gap-1 text-[10px]">
                {Object.entries(summary.data.severity_counts).map(
                  ([k, v]) => (
                    <span
                      key={k}
                      className="rounded bg-slate-100 px-1.5 py-0.5 font-medium text-slate-700"
                    >
                      {k}: {v}
                    </span>
                  ),
                )}
              </div>
            )}
          </header>
          <div className="flex-1 overflow-auto">
            {urls.isLoading ? (
              <div className="p-4 text-xs text-slate-500">Loading URLs…</div>
            ) : urls.isError ? (
              <div className="p-4 text-xs text-red-700">
                {(urls.error as Error).message}
              </div>
            ) : (
              <ul className="divide-y divide-slate-100">
                {(urls.data?.items ?? []).map((item) => (
                  <UrlListItem
                    key={item.url_id}
                    item={item}
                    selected={activeUrlId === item.url_id}
                    onSelect={() => setSelectedUrlId(item.url_id)}
                  />
                ))}
              </ul>
            )}
          </div>
        </div>
      )}

      {/* --- Detail (per-URL drill-in) ----------------------------------- */}
      <div className="flex-1 overflow-auto bg-slate-50">
        {selectedDate && selectedRunId && activeUrlId ? (
          <UrlDetail
            date={selectedDate}
            runId={selectedRunId}
            urlId={activeUrlId}
            onClose={() => setSelectedUrlId(null)}
          />
        ) : (
          <div className="p-8 text-sm text-slate-500">
            {selectedDate && selectedRunId
              ? 'Select a URL on the left to see its analysis.'
              : 'Pick a report on the left.'}
          </div>
        )}
      </div>
    </div>
  )
}

// --------------------------------------------------------------------------
// Sub-components
// --------------------------------------------------------------------------

function UrlListItem({
  item,
  selected,
  onSelect,
}: {
  item: ReportUrlSummary
  selected: boolean
  onSelect: () => void
}) {
  return (
    <li>
      <button
        onClick={onSelect}
        className={`block w-full px-4 py-2 text-left text-xs ${
          selected ? 'bg-blue-50' : 'hover:bg-slate-50'
        }`}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="truncate font-mono">{item.url_id}</span>
          {item.severity ? (
            <span
              className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                SEVERITY_STYLES[item.severity] ?? 'bg-slate-200 text-slate-700'
              }`}
            >
              {item.severity}
            </span>
          ) : (
            <span
              className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                RESULT_TYPE_STYLES[item.result_type]
              }`}
            >
              {item.result_type.replace('_', ' ')}
            </span>
          )}
        </div>
        {item.url && (
          <div className="mt-0.5 truncate text-[10px] text-slate-500">
            {item.url}
          </div>
        )}
      </button>
    </li>
  )
}

function UrlDetail({
  date,
  runId,
  urlId,
  onClose,
}: {
  date: string
  runId: string
  urlId: string
  onClose: () => void
}) {
  const detail = useReportUrlDetail(date, runId, urlId)
  // `screenshots` is the inventory of which kinds exist on disk; we
  // render <img> for each present kind. Memo so the URLs are stable
  // across re-renders (otherwise <img> would refetch on every keystroke).
  const screenshotUrls = useMemo(() => {
    if (!detail.data) return null
    const present = new Set(detail.data.screenshots)
    return {
      baseline: present.has('baseline')
        ? reportScreenshotUrl(date, runId, urlId, 'baseline')
        : null,
      current: present.has('current')
        ? reportScreenshotUrl(date, runId, urlId, 'current')
        : null,
      // The on-disk kind is `visual_diff`; the wire `which=` enum is
      // `diff` - `reportScreenshotUrl` maps to the right query string.
      diff: present.has('visual_diff')
        ? reportScreenshotUrl(date, runId, urlId, 'diff')
        : null,
    }
  }, [detail.data, date, runId, urlId])

  return (
    <div className="p-6">
      <div className="mb-4 flex items-start justify-between">
        <div>
          <div className="text-xs uppercase tracking-wide text-slate-500">
            URL
          </div>
          <h2 className="font-mono text-lg">{urlId}</h2>
        </div>
        <button
          onClick={onClose}
          className="rounded p-1 text-slate-400 hover:bg-white hover:text-slate-700"
          aria-label="Close detail"
        >
          ✕
        </button>
      </div>

      {detail.isLoading ? (
        <div className="text-sm text-slate-500">Loading detail…</div>
      ) : detail.isError || !detail.data ? (
        <div className="text-sm text-red-700">
          Failed to load: {(detail.error as Error)?.message ?? 'unknown'}
        </div>
      ) : (
        <>
          {/* --- Screenshots side-by-side ---
              For `no_changes` URLs the report stage skips screenshot
              copying entirely (test_ui/report/generator.py: writes only
              the marker file and returns early), so the three panels
              would all render as empty "(none)" boxes - misleading UX
              that suggests data is missing when in fact "no changes"
              is a deliberate, complete result. Replace with a one-line
              confirmation instead. */}
          {detail.data.result_type === 'no_changes' ? (
            <div className="mb-6 rounded-md border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-900">
              No visual changes detected; baseline and current matched
              pixel-for-pixel. Screenshots were not copied to the report
              dir (still available under{' '}
              <code className="rounded bg-white px-1 text-[11px]">
                data/baseline/
              </code>{' '}
              and{' '}
              <code className="rounded bg-white px-1 text-[11px]">
                data/current/
              </code>
              if you need to eyeball them).
            </div>
          ) : (
            screenshotUrls && (
              <div className="mb-6 grid grid-cols-3 gap-3">
                <ScreenshotPanel label="Baseline" src={screenshotUrls.baseline} />
                <ScreenshotPanel label="Current" src={screenshotUrls.current} />
                {/* The Diff panel shows nothing when the comparator
                    didn't detect a visual change (because the change
                    was HTML/CSS/JS-only). Default "(none)" was
                    confusing operators - replace with a contextual
                    note so they understand it's intentional. */}
                <ScreenshotPanel
                  label="Diff"
                  src={screenshotUrls.diff}
                  emptyMessage="No visual diff (change was in HTML / CSS / JS, not the rendering)"
                />
              </div>
            )
          )}

          {/* --- AI analysis JSON --- */}
          <div>
            <div className="mb-1 flex items-center gap-2">
              <span className="text-xs uppercase tracking-wide text-slate-500">
                AI analysis
              </span>
              <span
                className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                  RESULT_TYPE_STYLES[detail.data.result_type]
                }`}
              >
                {detail.data.result_type.replace('_', ' ')}
              </span>
            </div>
            <pre className="max-h-96 overflow-auto rounded-md bg-slate-900 p-3 text-[11px] leading-snug text-slate-100">
              {JSON.stringify(detail.data.analysis, null, 2)}
            </pre>
          </div>
        </>
      )}
    </div>
  )
}

function ScreenshotPanel({
  label,
  src,
  emptyMessage = '(none)',
}: {
  label: string
  src: string | null
  emptyMessage?: string
}) {
  return (
    <div className="overflow-hidden rounded-md border border-slate-200 bg-white">
      <div className="border-b border-slate-200 bg-slate-50 px-3 py-1.5 text-xs font-medium text-slate-700">
        {label}
      </div>
      {src ? (
        <a
          href={src}
          target="_blank"
          rel="noreferrer noopener"
          title="Open full-size in new tab"
        >
          <img
            src={src}
            alt={`${label} screenshot`}
            className="block max-h-72 w-full object-contain"
          />
        </a>
      ) : (
        <div className="flex h-32 items-center justify-center px-3 text-center text-xs text-slate-500">
          {emptyMessage}
        </div>
      )}
    </div>
  )
}
