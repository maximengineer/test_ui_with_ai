# Backlog

Forward-looking work known to the project but not currently in flight.
Extracted from `REFACTOR_AND_DASHBOARD_PLAN.md` once Milestones A/B/C
shipped (full historical plan archived under [docs/history/](docs/history/)).

For *system architecture* see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Architecture improvement backlog

These are the current follow-ups after the main refactor phases. Add new
architecture work here, not as a second architecture plan file.

### Security checks and privacy hardening

1. **SSRF hardening beyond static URL validation.**
   - Current behavior blocks private/link-local/loopback IP literals and localhost-style hostnames by default.
   - Crawler-time preflight checks DNS-resolved private targets and redirect chains before browser/resource fetches.
   - Docker runs can enable container-level egress allowlisting with `AFR_EGRESS_ALLOWLIST_ENABLED=true`.
   - Remaining risk: host-native local runs do not get a repo-managed firewall, and browser-level JavaScript navigation can still fail captures if required destinations are not allowlisted.
2. **Sensitive artifact controls beyond structured-text redaction.**
   - Current behavior masks obvious secrets in structured DOM/CSS/JS diff strings before AI calls and report persistence.
   - `AFR_AI_REDACT_SCREENSHOTS=true` keeps screenshots out of AI payloads while preserving local report screenshots.
   - Remaining risk: raw captured pages/comparator artifacts, screenshots in local reports, and non-obvious secrets can still contain PII or confidential content.
   - Add raw-artifact redaction, OCR/region redaction, or explicit site data allow-listing before using the tool on confidential pages.
3. **Deterministic security floors.**
   - Continue expanding report-side severity floors for structured HTML/CSS/JS markers.
   - Prioritize tests for attacker URLs, inline event handlers, CSP/SRI stripping, hidden content, JavaScript execution sinks, cookie/storage/network markers, and visual-only changes.
4. **Prompt/rule single source of truth.**
   - The analyzer prompt and `server.js` prioritization rules should be generated from shared constants so severity ordering cannot drift silently.

### Refactoring and simplification

1. **Crawler engine split.**
   - `test_ui/crawler/engine.py` still owns capture, asset extraction, persistence, and metadata concerns.
   - Split only when adding crawler behavior; preserve artifact layout and naming.
2. **Dashboard DB/runner services.**
   - `dashboard/api/db.py` and `dashboard/api/runner.py` are still large but cohesive.
   - Split around retention, migration, subprocess spawning, and recovery only when tests can pin behavior.
3. **Large React pages.**
   - Continue decomposing `SitesPage`, `ReportsPage`, and `RunsPageView` into view, state, and action modules.
   - Keep generated OpenAPI types as the contract source instead of adding manual enum/string unions.

### Determinism and noise reduction

1. Improve crawler determinism only in response to observed noise patterns:
   - animation disabling
   - dynamic-region masking
   - cookie/session stabilization
   - font consistency
   - stricter timezone/runtime controls
2. Keep SSIM-only visual diffing unless real false positives prove color-aware diffing is worth the extra complexity.

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
- **Full raw-artifact redaction as a default-on feature.** AI requests and report `structured_data.json` get best-effort structured-text masking, and `AFR_AI_REDACT_SCREENSHOTS=true` can keep screenshots out of AI payloads. Raw crawler/comparator DOM, CSS, JS, and screenshot artifacts remain unredacted. For sensitive pages, the safest privacy control is explicit opt-out via `AFR_AI_ENABLED=false`.
- **`RunExecutor` abstract interface.** No second implementation in sight; YAGNI.
- **`run_events` audit table.** Per-run log file is sufficient.
- **Windows support for the dashboard subprocess job runner.** CLI works on macOS; dashboard is Linux-only (or Linux-in-Docker) because it reads `/proc/<pid>/stat`.
- **Replacing Crawl4AI custom asset-downloading regex** in [test_ui/crawler/engine.py](test_ui/crawler/engine.py).
