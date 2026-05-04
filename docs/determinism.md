# Crawler determinism baseline

UI regression analysis is only as useful as its inputs are deterministic. Every source of noise in the crawl becomes a false positive that the AI layer then has to explain away. This doc records what determinism controls we use today, what we *don't* control, and which gaps are most likely to produce noisy diffs. Comprehensive determinism work is out of scope for the current refactor - see `REFACTOR_AND_DASHBOARD_PLAN.md` non-goals - but knowing the gaps is the prerequisite for fixing them later.

## What we control today

Source: [`test_ui/crawler/engine.py`](../test_ui/crawler/engine.py) and [`test_ui/config.py`](../test_ui/config.py).

| Control | Value | How |
|---|---|---|
| Browser engine | Chromium | Crawl4AI default |
| Headless mode | true | `AFR_BROWSER_HEADLESS=true`; `Settings.browser_headless` |
| Viewport width | 1920 | `Settings.viewport_width` |
| Viewport height | 1080 | `Settings.viewport_height` |
| Page timeout | 45s | `Settings.crawler_timeout` (was 30; bumped for 30–40 page crawls) |
| Parallel workers | 3 | `Settings.crawler_workers` |
| Asset downloading | hash-stripped, deterministic naming | crawler rewrites HTML to use deterministic local filenames so hash-based CDN URLs don't produce false diffs |

The hash-stripping is genuinely valuable - without it, every cache-busted CSS link looks like a "change."

## What we don't control (sources of noise)

These are the most likely producers of false-positive diffs. Listed roughly by how often they bite, not by difficulty to fix.

1. **Animations and transitions.** A screenshot taken mid-fade or mid-slide differs from one taken before/after. No `prefers-reduced-motion` is set; no JS-based animation freezing.
2. **Dynamic content (carousels, tickers, ads, recommendations).** Different content on each crawl by design.
3. **Timezone / locale.** Server-rendered timestamps, currency, date formats can vary by container locale. We don't pin either.
4. **Cookie / session state.** Each crawl starts cold. Logged-in vs logged-out states aren't supported.
5. **A/B tests and feature flags.** Random bucket assignment per crawl produces structural diffs we can't attribute to a "change."
6. **Third-party scripts.** Analytics, fonts, embedded widgets - all change asynchronously and outside our control.
7. **Font loading races.** FOIT/FOUT can change the *first* screenshot vs subsequent ones; no font preload waits.
8. **Network conditions.** Slow CDN responses can leave images unloaded at screenshot time. No explicit "wait for network idle" beyond Crawl4AI defaults.
9. **Browser version pinning.** Crawl4AI's bundled Chromium version is whatever the installed `crawl4ai` package gives us. No explicit pin.
10. **Per-page ignore regions / masks.** No way to say "ignore this DOM subtree in diffs" - useful for known-dynamic regions like timestamps and ad slots.

## Why this matters for the AI

The AI layer can confidently describe what changed visually. It can't tell you whether the change is a regression, a deliberate update, or noise. If the underlying inputs are noisy, the analysis is polished noise.

Two examples:
- A carousel that shows a different image on each crawl: AI flags "hero image changed from X to Y." The user sees this happen on every run regardless of any deploy.
- A timestamp in the footer ("Last updated: 5 minutes ago"): AI flags "footer text changed." Again, every crawl.

## Out of scope for the current refactor

All items in the "what we don't control" list are deferred. They show up in the main plan's deferred work. Adding any of them requires changes to the crawler, which is currently a non-goal. This doc exists so that when those changes are picked up later, the starting point is documented rather than rediscovered.
