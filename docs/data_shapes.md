# Input data shapes

Documents the actual shape of the structured-diff JSON files the comparator produces and the AI analyzer consumes. This drives the Pydantic contract models in `test_ui/contracts/ai_contract.py` (Phase A.1.2) and the Node prompt construction (Phase A.1.4).

> **Source of truth:** [`test_ui/comparator/engine.py`](../test_ui/comparator/engine.py). The shapes documented here are derived from reading the producer code, not from observing live data (no real comparator output existed at the time of writing). Synthetic example fixtures live in [`tests/fixtures/example_diffs/`](../tests/fixtures/example_diffs/) and match these shapes.

## Files emitted per URL

The comparator writes per-URL outputs to:

```
data/comparator/<DD-MM-YYYY>/<url_dir>/
├── comparison_results.json     # always written, even on error or no-change
└── diffs/                      # only created if changes are detected
    ├── change_summary.json     # AI-facing master summary
    ├── html_changes.json       # DOM diff details
    ├── css_changes.json        # CSS file diff
    ├── js_changes.json         # JS file diff
    └── visual_diff.png         # SSIM-based visual diff (binary, not part of structured data)
```

If the comparator detects no changes (no visual diff, no DOM diff, no asset changes), `diffs/` is **not created**. The report layer's discovery code uses the presence of `diffs/` and the `changes_detected` field in `comparison_results.json` to decide whether AI analysis should be invoked.

---

## `comparison_results.json`

Always written, regardless of outcome. The wrapper around per-URL state.

```json
{
  "metadata": {
    "timestamp": "30-04-2026 14:23:11",
    "url": "https://example.com/about",
    "baseline_path": "/abs/path/to/data/baseline/30-04-2026",
    "current_path": "/abs/path/to/data/current/30-04-2026",
    "output_path": "/abs/path/to/data/comparator/30-04-2026/example.com_about"
  },
  "result": { ... }   // see below
}
```

### `result` field — three shapes

**Error case** (site missing from baseline or current):

```json
{
  "url": "https://example.com/about",
  "error": "missing_baseline",          // or "missing_current"
  "message": "Site not found in baseline"
}
```

**Success case** (full shape, derived from [`_compare_single_site`](../test_ui/comparator/engine.py#L1290) returning the dicts emitted by `_compare_dom`, `_compare_assets`, `_compare_screenshots`):

```json
{
  "url": "https://example.com/about",
  "changes_detected": true,
  "diffs_created": true,
  "screenshot": {
    "ssim_score": 0.87,
    "visual_changes": true,
    "dimensions_changed": false,
    "diff_image_path": "/abs/path/.../diffs/visual_diff.png"
  },
  "dom": {
    "title": { "baseline": "...", "current": "...", "changed": true },
    "structure": {
      "element_changes": [...],
      "specific_changes": [...],
      "tag_counts": { "baseline": {...}, "current": {...} }
    },
    "content": {
      "baseline_length": 4521,
      "current_length": 5832,
      "length_change": 1311,
      "significant_change": true
    },
    "meta": { "changes": [...] },
    "navigation": { "changes": [...] },
    "has_changes": true
  },
  "assets": {
    "css":   { "added": [...], "removed": [...], "changed": [...], "has_changes": true, "total_changes": 2, "content_changes": [...], "detailed_analysis": {...} },
    "js":    { "added": [], "removed": [], "changed": [], "has_changes": false, "total_changes": 0, "content_changes": [], "detailed_analysis": {} },
    "media": { ...same shape... }
  }
}
```

Consumed by [`report/generator.py:42-54`](../test_ui/report/generator.py#L42-L54) — note the multi-signal change detection there, which OR-s `changes_detected` against several other fields. Phase A.3 should investigate why the top-level `changes_detected` flag isn't trusted on its own.

> **⚠ Known comparator bug:** [`_create_change_summary_json`](../test_ui/comparator/engine.py#L1176-L1178) reads `dom_result.get("title_changed", False)`, `.get("content_changed", False)`, `.get("structure_changed", False)` — but `_compare_dom` doesn't emit those flat keys; it emits nested objects (`title.changed`, `content.significant_change`, etc.). So the `change_categories.content` block in every real `change_summary.json` always shows all-`False`. Out of scope for A.1; flagged for the comparator-side cleanup in A.3.

---

## `change_summary.json`

The master AI-facing summary. Used to drive prioritization in the Node prompt.

```json
{
  "overall_assessment": {
    "changes_detected": true,
    "change_severity": "high",         // "none" | "low" | "medium" | "high"
    "user_impact": "high",             // "none" | "low" | "medium" | "high"
    "requires_review": true
  },
  "change_categories": {
    "visual": {
      "screenshot_similarity": 0.87,
      "visual_changes": true,
      "layout_shifts": false
    },
    "content": {
      "title_changed": false,
      "text_content_changed": true,
      "structure_changed": true
    },
    "technical": {
      "html_changes": true,
      "css_changes": true,
      "js_changes": false,
      "asset_changes": false
    }
  },
  "affected_components": ["visual_layout", "content", "structure", "styling"],
  "recommendation": "Review visual changes in layout; Verify styling consistency",
  "ai_analysis_priority": "high"
}
```

Severity assignment, from [`engine.py:1102-1128`](../test_ui/comparator/engine.py#L1102-L1128):
- Visual: SSIM `< 0.8` → high; `< 0.95` → medium; else low.
- CSS changes → medium contribution.
- JS changes → high contribution.
- HTML changes → low contribution (unless structural — but the structural distinction isn't captured at this level).
- Overall = max of contributions, with `none` if no category fired.

---

## `html_changes.json`

Per-change records describing the DOM diff. **This is the file with per-change records and code snippets** — the most important file for AI prompt construction.

```json
{
  "changes_detected": true,
  "change_types": ["content", "structure", "attributes"],
  "changes": [
    {
      "type": "content",                    // "content" | "structure" | "structure_detail" | "attributes"
      "element": "title",
      "change": "text_modified",
      "description": "Page title changed",
      "old_value": "Welcome | Acme",
      "new_value": "About Us | Acme",
      "impact": "medium"                    // "low" | "medium" | "high"
    },
    {
      "type": "structure",
      "element": "div",
      "change": "added_element",
      "description": "Added 3 div element(s)",
      "old_value": 12,
      "new_value": 15,
      "impact": "low",
      "code_examples_count": 3              // only present on aggregate structure changes
    },
    {
      "type": "structure_detail",
      "element": "section.hero",
      "change": "element_added",
      "description": "New hero section inserted before <main>",
      "code_snippet": "<section class=\"hero\"><h1>About</h1><p>...</p></section>",
      "position": "before:main",
      "impact": "high"
    },
    {
      "type": "attributes",
      "element": "meta[viewport]",
      "change": "meta_modified",
      "description": "Meta tag 'viewport' modified",
      "old_value": "width=device-width, initial-scale=1",
      "new_value": "width=device-width, initial-scale=1.0, maximum-scale=1.0",
      "impact": "low"
    }
  ],
  "summary": {
    "total_changes": 4,
    "structural_changes": 2,
    "content_changes": 1,
    "meta_changes": 1,
    "navigation_changes": 0,
    "high_impact_changes": 1,
    "medium_impact_changes": 1,
    "severity": "high"
  }
}
```

### Important field contracts

- **`changes` is a list of records.** Each has `type`, `element`, `change`, `description`, `impact`. Optional fields depend on the `type`.
- **Per-change `impact`** is `"low" | "medium" | "high"` and is the natural prioritization key for prompt truncation (Phase A.1.4).
- **`code_snippet`** is only present on `type: "structure_detail"` records. Length is bounded by an internal `_clean_html_snippet` cap (~300 chars in current code). Phase A.1.4's "max 2000 chars per snippet" cap is therefore non-binding on output today; the comparator already truncates more aggressively. Worth confirming when real data exists.

---

## `css_changes.json`

```json
{
  "changes_detected": true,
  "change_types": ["layout", "styling"],
  "files_changed": ["main.css", "theme.css"],
  "changes": [
    {
      "file": "header.css",
      "change_type": "added",        // "added" | "removed" | "modified"
      "description": "New CSS file added: header.css",
      "impact": "layout",            // always "layout" today
      "severity": "medium"
    },
    {
      "file": "old-theme.css",
      "change_type": "removed",
      "description": "CSS file removed: old-theme.css",
      "impact": "layout",
      "severity": "high"
    }
  ],
  "summary": {
    "total_changes": 2,
    "layout_affecting": 2,
    "visual_only": 0,
    "severity": "high"
  }
}
```

> **Important limitation:** `css_changes.json` operates at the **file** level, not the rule/property level. There is no per-selector or per-property diff. The AI sees "main.css changed" but not "the `.btn-primary` background-color changed from blue to green." This is a known gap; the comparator's CSS analysis is shallow.

## `js_changes.json`

Same structure as `css_changes.json` but with `functionality_impact` instead of CSS-specific fields:

```json
{
  "changes_detected": false,
  "change_types": [],
  "files_changed": [],
  "changes": [
    {
      "file": "analytics.js",
      "change_type": "modified",
      "description": "JavaScript file modified: analytics.js",
      "functionality_impact": "medium"   // "low" | "medium" | "high"
    }
  ],
  "summary": {
    "total_changes": 1,
    "functionality_impact": "medium",
    "severity": "medium"
  }
}
```

> Same file-level-only limitation as CSS: no per-function or per-symbol diff visible to the AI.

---

## Prompt-design implications (driving Phase A.1.4)

Since `html_changes.json` is the only file with per-change records and code snippets, it's where prompt prioritization matters. Concrete rules for Phase A.1.4:

1. **Prioritize HTML changes by `impact`:** `high` → `medium` → `low`. Within a tier, preserve order (the comparator already groups title → element changes → specific structure → meta → navigation → content, which is roughly impact-descending).
2. **Prioritize `type: "structure_detail"` over `type: "structure"`:** the `_detail` records carry actual code snippets; the aggregate records only carry counts.
3. **CSS / JS payloads are small** (file-level only). Send all entries.
4. **The 200-changes-per-category cap from the plan is conservative** for HTML and unused for CSS/JS at current data shapes. Worth measuring on real data.

### What does NOT exist (model can't ask for it)

- Per-CSS-rule diffs (which selectors / properties changed).
- Per-JS-function diffs.
- Pixel-level coordinates of visual differences (only an SSIM score and a diff image).
- Cross-file dependency analysis ("this CSS change broke this HTML element").

If the AI's analysis quality requires any of these, the comparator itself needs work — and that's outside Milestone A's non-goals. Document the gap, plan future work.

---

## Realistic payload sizes

**Cannot measure today** — no live data exists. Phase A.1.4's bounded defaults (30 MB body, 200 changes per category, 2000-char snippets) are sized for safety, not measurement. After the first real run produces output, this section should be updated with observed sizes for a representative spread of pages (small/medium/large).
