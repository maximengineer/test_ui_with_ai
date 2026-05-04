# Example diff fixtures

Synthetic examples of the JSON files the comparator produces, matching the shapes documented in [`docs/data_shapes.md`](../../../docs/data_shapes.md).

These are **hand-constructed**, not captured from real comparator output. They exist so:

1. Phase A.1 has reference inputs for the Pydantic contract models without needing a live crawl.
2. Phase A.4 cross-language schema tests have valid sample payloads.
3. Phase A.2 characterization tests can use them as fake comparator output.

When real comparator data exists, replace these with sanitized samples from real runs and update [`docs/data_shapes.md`](../../../docs/data_shapes.md) with observed payload sizes.

## Files

- `comparison_results.json` - wrapper file, success case with detected changes.
- `change_summary.json` - master AI-facing summary.
- `html_changes.json` - DOM diff with several change types.
- `css_changes.json` - file-level CSS diff.
- `js_changes.json` - file-level JS diff (no detected changes - exercises the empty path).
