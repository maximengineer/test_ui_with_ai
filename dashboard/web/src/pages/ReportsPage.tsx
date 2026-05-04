/**
 * Reports page: pick date + run → see URL list with severity → drill into a
 * single URL to see baseline/current/diff screenshots side-by-side and
 * the AI analysis JSON.
 *
 * Layout: 3-column when a URL is selected (date+run picker | URL list |
 * detail panel). Without a selection: 2 columns. Without a run picked:
 * just the date+run picker on the left.
 */
import { useEffect, useMemo, useState } from 'react'

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
  const reportDates = dates.data?.report ?? []
  const [date, setDate] = useState<string | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const [selectedUrlId, setSelectedUrlId] = useState<string | null>(null)

  const runs = useReportRuns(date)
  const summary = useReportSummary(date, runId)
  const urls = useReportUrls(date, runId)

  // When the date list arrives, default to the newest date so the page
  // is useful immediately. Selecting a different date clears the run +
  // URL selection so we don't render a mismatched detail panel.
  useEffect(() => {
    if (date === null && reportDates.length > 0) {
      setDate(reportDates[0])
    }
  }, [date, reportDates])

  // Once a date is picked, default-select its newest run.
  useEffect(() => {
    if (runs.data && runs.data.length > 0 && runId === null) {
      setRunId(runs.data[0].run_id)
    }
  }, [runs.data, runId])

  return (
    <div className="flex h-full">
      {/* --- Date + run picker (always visible) ------------------------- */}
      <div className="flex w-64 flex-col border-r border-slate-200 bg-white">
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
                      date === d
                        ? 'bg-slate-900 text-white'
                        : 'text-slate-700 hover:bg-slate-100'
                    }`}
                  >
                    {d}
                  </button>
                  {date === d && runs.data && runs.data.length > 0 && (
                    <ul className="ml-2 mt-1 space-y-0.5 border-l border-slate-200 pl-2">
                      {runs.data.map((r) => (
                        <li key={r.run_id}>
                          <button
                            onClick={() => {
                              setRunId(r.run_id)
                              setSelectedUrlId(null)
                            }}
                            className={`w-full truncate rounded px-2 py-1 text-left font-mono text-[10px] ${
                              runId === r.run_id
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

      {/* --- URL list ---------------------------------------------------- */}
      {date && runId && (
        <div className="flex w-96 flex-col border-r border-slate-200 bg-white">
          <header className="border-b border-slate-200 px-4 py-3">
            <div className="text-xs uppercase tracking-wide text-slate-500">
              Run
            </div>
            <div className="font-mono text-xs">{runId}</div>
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
                    selected={selectedUrlId === item.url_id}
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
        {date && runId && selectedUrlId ? (
          <UrlDetail
            date={date}
            runId={runId}
            urlId={selectedUrlId}
            onClose={() => setSelectedUrlId(null)}
          />
        ) : (
          <div className="p-8 text-sm text-slate-500">
            {date && runId
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
          {/* --- Screenshots side-by-side --- */}
          {screenshotUrls && (
            <div className="mb-6 grid grid-cols-3 gap-3">
              <ScreenshotPanel label="Baseline" src={screenshotUrls.baseline} />
              <ScreenshotPanel label="Current" src={screenshotUrls.current} />
              <ScreenshotPanel label="Diff" src={screenshotUrls.diff} />
            </div>
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
}: {
  label: string
  src: string | null
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
        <div className="flex h-32 items-center justify-center text-xs text-slate-400">
          (none)
        </div>
      )}
    </div>
  )
}
