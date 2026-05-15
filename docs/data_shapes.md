# Input data shapes

Documents the actual comparator output shapes consumed by the report and AI
layers.

## Scope and source of truth

This file describes on-disk JSON produced by the comparator stage:

- `test_ui/comparator/engine.py`
- `test_ui/comparator/dom.py`
- `test_ui/comparator/assets.py`
- `test_ui/comparator/summary.py`

The report stage reads these files via:

- `test_ui/report/discovery.py`
- `test_ui/report/loader.py`

If code and this doc diverge, code wins.

## Comparator output layout

Per-site outputs are written to:

```text
data/comparator/<DD-MM-YYYY>/<run_id>/<site_id>/
├── comparison_results.json
└── diffs/                  # present only when changes are detected
    ├── change_summary.json
    ├── html_changes.json
    ├── css_changes.json
    ├── js_changes.json
    └── visual_diff.png
```

Notes:

- `comparison_results.json` is always written (including comparator error cases).
- `diffs/` is created only when the comparator reports `changes_detected=true`.
- `site_id` is the stable id from `sites.yml` (`test_ui/common/sites.py`).

## comparison_results.json

Top-level wrapper for one site comparison.

```json
{
  "metadata": {
    "timestamp": "30-04-2026 14:23:11",
    "url": "https://example.com/about",
    "site_id": "12",
    "baseline_path": "/abs/path/to/data/baseline/30-04-2026/01...",
    "current_path": "/abs/path/to/data/current/30-04-2026/01...",
    "output_path": "/abs/path/to/data/comparator/30-04-2026/01.../12"
  },
  "result": {}
}
```

### result: error shape

Used when the comparator cannot perform a valid comparison for the site.

```json
{
  "url": "https://example.com/about",
  "error": "missing_baseline",
  "message": "Site not found in baseline"
}
```

Observed `error` values include:

- `missing_baseline`
- `missing_current`
- `Comparison failed: ...` (unexpected comparator exception)

### result: success shape

```json
{
  "url": "https://example.com/about",
  "screenshot": {
    "ssim_score": 0.87,
    "visual_changes": true,
    "dimensions_changed": false,
    "diff_image_path": "/abs/path/.../diffs/visual_diff.png"
  },
  "assets": {
    "css": { "has_changes": true, "added": [], "removed": [], "changed": [] },
    "js": { "has_changes": false, "added": [], "removed": [], "changed": [] },
    "media": { "has_changes": false, "added": [], "removed": [], "changed": [] }
  },
  "dom": {
    "title": { "baseline": "...", "current": "...", "changed": true },
    "structure": { "element_changes": [], "specific_changes": [], "tag_counts": {} },
    "content": { "baseline_length": 4521, "current_length": 5832, "length_change": 1311, "significant_change": true },
    "meta": { "changes": [] },
    "navigation": { "changes": [] },
    "key_attributes": { "changes": [] },
    "dynamic_attributes": { "changes": [] },
    "headings": { "changes": [] },
    "has_changes": true
  },
  "changes_detected": true,
  "diffs_created": true
}
```

Notes:

- `changes_detected` is the report-discovery signal for changed URLs.
- Comparator error payloads (`result.error`) are still routed to report processing so report output records the failure clearly.

## change_summary.json

High-level AI-facing rollup from `summary.create_change_summary_json()`.

```json
{
  "overall_assessment": {
    "changes_detected": true,
    "change_severity": "high",
    "user_impact": "high",
    "requires_review": true
  },
  "change_categories": {
    "visual": {
      "screenshot_similarity": 0.87,
      "visual_changes": true,
      "layout_shifts": false
    },
    "content": {
      "title_changed": true,
      "text_content_changed": true,
      "structure_changed": true,
      "attribute_changes": 3,
      "dynamic_attribute_changes": 1,
      "heading_changes": 2,
      "meta_changes": 1
    },
    "technical": {
      "html_changes": true,
      "css_changes": true,
      "js_changes": false,
      "asset_changes": false
    }
  },
  "affected_components": [
    "content",
    "headings",
    "links_and_navigation",
    "styling",
    "visual_layout"
  ],
  "recommendation": "Review visual changes in layout; Verify styling consistency; ...",
  "ai_analysis_priority": "high"
}
```

Key behavior:

- Severity is rolled up from per-signal impacts (`none|low|medium|high`).
- `affected_components` is sorted for deterministic output.
- HTML-derived signals now include attribute and heading-level detail, not only title/content/structure booleans.

## html_changes.json

Detailed DOM diff projection from `dom.create_html_changes_json()`.

```json
{
  "changes_detected": true,
  "change_types": ["content", "structure", "attributes"],
  "changes": [
    {
      "type": "content",
      "element": "title",
      "change": "text_modified",
      "description": "Page title changed",
      "old_value": "Welcome | Acme",
      "new_value": "About | Acme",
      "impact": "medium"
    },
    {
      "type": "structure_detail",
      "element": "section.hero",
      "change": "element_added",
      "description": "New hero section inserted",
      "code_snippet": "<section class=\"hero\">...</section>",
      "position": "before:main",
      "impact": "high"
    },
    {
      "type": "attributes",
      "element": "a[4].href",
      "change": "attribute_modified",
      "description": "Attribute a[4].href modified",
      "old_value": "https://trusted.example",
      "new_value": "https://attacker.example",
      "impact": "high"
    }
  ],
  "summary": {
    "total_changes": 3,
    "structural_changes": 0,
    "content_changes": 1,
    "meta_changes": 1,
    "navigation_changes": 0,
    "high_impact_changes": 2,
    "medium_impact_changes": 1,
    "severity": "high"
  }
}
```

Notes:

- `changes[].type` values include `content`, `structure`, `structure_detail`, `attributes`.
- `impact` uses `low|medium|high` and is used downstream in severity rollups.
- `structure_detail` records can carry `code_snippet` and `position`.

## css_changes.json

Projection from `assets.create_css_changes_json()`.

```json
{
  "changes_detected": true,
  "change_types": ["layout", "styling"],
  "files_changed": ["main.css"],
  "changes": [
    {
      "file": "main.css",
      "change_type": "modified",
      "description": "CSS file modified: main.css",
      "impact": "layout",
      "severity": "medium"
    },
    {
      "file": "main.css",
      "change_type": "selector_modified",
      "description": "CSS selector .cta modified",
      "selector": ".cta",
      "impact": "high",
      "property_changes": ["background-color", "color"],
      "code_snippet": ".cta { ... }"
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

Notes:

- Includes file-level records and selected per-rule content changes.
- Per-rule entries are filtered/capped to avoid prompt bloat.

## js_changes.json

Projection from `assets.create_js_changes_json()`.

```json
{
  "changes_detected": true,
  "change_types": ["functionality"],
  "files_changed": ["app.js"],
  "changes": [
    {
      "file": "app.js",
      "change_type": "modified",
      "description": "JavaScript file modified: app.js",
      "functionality_impact": "medium"
    },
    {
      "file": "app.js",
      "change_type": "function_modified",
      "description": "js_function_modified handleSubmit",
      "function_name": "handleSubmit",
      "impact": "high",
      "code_snippet": "function handleSubmit(...) { ... }"
    }
  ],
  "summary": {
    "total_changes": 2,
    "functionality_impact": "high",
    "severity": "high"
  }
}
```

Notes:

- Includes file-level records and selected function/security-indicator records.
- Function-level entries are filtered/capped and snippets truncated.

## Report-stage consumption notes

- `report/discovery.py` buckets URLs by `result.changes_detected` and comparator error presence.
- `report/loader.py` reads `diffs/*.json` into `structured_data` and attaches screenshots.
- Missing/corrupt per-file diffs are represented as per-key error markers instead of aborting the whole URL.

## Contract update checklist

When comparator output shape changes:

1. Update this doc (`docs/data_shapes.md`).
2. Update affected report-loader and/or template assumptions.
3. Update or regenerate fixtures and tests (`tests/test_comparator_*`, `tests/test_discovery.py`, `tests/test_report_*`).
4. If AI request/response wire contracts changed, also regenerate schemas with `python scripts/export_schemas.py`.
