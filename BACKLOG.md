# Backlog

Forward-looking work known to the project but not currently in flight.
Extracted from `REFACTOR_AND_DASHBOARD_PLAN.md` once Milestones A/B/C
shipped (full historical plan archived under [docs/history/](docs/history/)).

For *system architecture* see [ARCHITECTURE.md](ARCHITECTURE.md).
For *operating instructions to AI agents editing this code* see [CLAUDE.md](CLAUDE.md).

---

## Upstream-blocked dependency upgrades

| Package | Pinned at | Wanted | Why blocked |
|---|---|---|---|
| `lxml` | 5.4.0 (`<6.0.0`) | 6.x | `crawl4ai 0.8.6`'s transitive deps (patchright, etc.) require `lxml < 6`. Re-check on every `crawl4ai` release. |

---

## Known latent bugs

Pinned in tests with explicit "LATENT BUG" docstrings so they can't drift unnoticed. Real-world impact today is near-zero; addressing them is a clean-up pass, not a hot fix.

### `url_to_dirname` ([test_ui/common/url_id.py](test_ui/common/url_id.py))

Four edge cases pinned in [tests/test_url_id.py](tests/test_url_id.py):

1. `www.` is replaced *globally*, not just at the prefix anchor. `www.foo.www.bar.com` collapses to `foo.bar.com`.
2. Port appears in dirname as `host:port_path`. The `:` is illegal on Windows / NTFS.
3. Host case is preserved verbatim. `EXAMPLE.com` and `example.com` collide on case-insensitive filesystems and split on case-sensitive ones.
4. Percent-encoded paths are not decoded. `caf%C3%A9` and `café` produce different dirnames for the same URL.

A future canonicalizer pass should `.lower()` the netloc and `unquote()` the path. Today's site sets don't trip any of these (Linux filesystems, no port collisions in `sites.yml`).

### Prompt-prioritization rules duplicated

The "structural HTML first → content → CSS/JS" prioritization order is enforced in [ai_analyzer/server.js](ai_analyzer/server.js) (`prioritizeStructuredData`) **and** described in [ai_analyzer/prompts/system.txt](ai_analyzer/prompts/system.txt). If we change one and forget the other, they silently disagree. Fix is to template the prompt at startup with constants from `server.js` so they can't drift.

---

## Intentionally out of scope

Things you might expect but won't find. Listed so they aren't re-litigated each time someone notices the gap.

- **Streaming uploads instead of base64-in-JSON for screenshots.** Bounded at 50 MB body / 10 MB per image is sufficient.
- **Color-aware visual diffing.** SSIM-only.
- **Server-side rate limiting on the AI analyzer.** Client-side semaphore + `Retry-After` is enough for our scale.
- **Comprehensive crawler determinism** (timezone pinning, animation disabling, A/B stabilization, font consistency, dynamic-region masking). [docs/determinism.md](docs/determinism.md) documents the gaps.
- **"Diff across arbitrary dates / runs" feature.** Latest-vs-latest only.
- **Notification integrations** (Slack/email).
- **Real-time log streaming via SSE/WebSocket.** Dashboard polls `/api/runs/{id}` every 2s.
- **Multi-user auth and RBAC.** Single-user; dashboard binds `127.0.0.1` by default.
- **Subprocess re-attachment after dashboard restart.** Restart marks running rows `interrupted`; would need wrapper-script + exit-marker design.
- **Run cancellation API** (`POST /api/runs/{id}/cancel`).
- **Playwright browser tests for the dashboard.** API-level smoke only.
- **DOM/screenshot redaction.** Opt-out via `AFR_AI_ENABLED=false` instead.
- **SSRF protection on dashboard-triggered crawls** (block link-local, RFC1918, metadata endpoints).
- **`RunExecutor` abstract interface.** No second implementation in sight; YAGNI.
- **`run_events` audit table.** Per-run log file is sufficient.
- **Windows support for the dashboard subprocess job runner.** CLI works on macOS; dashboard is Linux-only (or Linux-in-Docker) because it reads `/proc/<pid>/stat`.
- **Replacing Crawl4AI custom asset-downloading regex** in [test_ui/crawler/engine.py](test_ui/crawler/engine.py).
