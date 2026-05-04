"""discovery.discover_comparison_data tests (Phase A.4).

Pins the bucketing of per-URL comparator output into changed/unchanged. The
function is pure-over-filesystem, so every test lays out a synthetic tree
under `tmp_path` and asserts on the returned dict.

Particularly important to cover:
  - Resilience: missing date dir, missing comparison_results.json, malformed
    JSON, non-dir entries - none should raise; bad URLs are skipped.
  - The single-source-of-truth check: A.3 simplified the discovery logic to
    trust `result.changes_detected` (was OR-ing 6 fields). These tests pin
    that simplification - ensure a URL with `changes_detected=true` lands in
    `with_changes` even when per-category flags are all False, and vice versa.
  - The `structured_data_path` is None when the diffs/ subdir is absent
    (happens when the comparator detected changes via screenshot only and
    didn't write per-category JSON yet - rare but possible).
"""

from __future__ import annotations

import json
from pathlib import Path

from test_ui.common.manifest import Manifest, write_manifest
from test_ui.report.discovery import discover_comparison_data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_url_dir(
    date_root: Path,
    url_name: str,
    *,
    changes_detected: bool | None = None,
    create_diffs_dir: bool = True,
    extra_result: dict | None = None,
    raw_json: str | None = None,
):
    """Lay down `<date_root>/<url_name>/comparison_results.json`.

    `changes_detected=None` omits the field entirely (simulates pre-A.1
    output or error-path comparator results that have no `result` block).
    `raw_json` overrides the body completely - for malformed-JSON tests.
    `create_diffs_dir=False` skips creating the `diffs/` subdir.
    """
    url_dir = date_root / url_name
    url_dir.mkdir(parents=True)
    if create_diffs_dir:
        (url_dir / "diffs").mkdir()

    if raw_json is not None:
        (url_dir / "comparison_results.json").write_text(raw_json, encoding="utf-8")
        return

    result: dict = {}
    if changes_detected is not None:
        result["changes_detected"] = changes_detected
    if extra_result:
        result.update(extra_result)

    payload = {
        "metadata": {"url": f"https://example.com/{url_name}"},
        "result": result,
    }
    (url_dir / "comparison_results.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Empty / missing inputs
# ---------------------------------------------------------------------------


def test_returns_empty_buckets_when_date_dir_missing(tmp_path):
    """Missing `<root>/<date>/` → returns `{with_changes: [], without_changes: []}`.

    Crucially, must NOT raise - the report stage may run before the comparator
    has produced anything for the requested date, and we want the report stage
    to handle that gracefully.
    """
    result = discover_comparison_data(tmp_path, "01-01-2099")
    assert result == {"with_changes": [], "without_changes": []}


def test_returns_empty_buckets_when_date_dir_empty(tmp_path):
    """Empty date dir is a valid state (no URLs crawled yet)."""
    (tmp_path / "01-01-2099").mkdir()
    result = discover_comparison_data(tmp_path, "01-01-2099")
    assert result == {"with_changes": [], "without_changes": []}


# ---------------------------------------------------------------------------
# Bucketing - the post-A.3 single-source-of-truth check
# ---------------------------------------------------------------------------


def test_changes_detected_true_lands_in_with_changes(tmp_path):
    date = "02-05-2026"
    date_root = tmp_path / date
    _seed_url_dir(date_root, "site_a", changes_detected=True)

    result = discover_comparison_data(tmp_path, date)

    assert len(result["with_changes"]) == 1
    assert len(result["without_changes"]) == 0
    entry = result["with_changes"][0]
    assert entry["url_name"] == "site_a"
    assert entry["has_changes"] is True
    assert entry["url_dir"] == date_root / "site_a"
    assert entry["structured_data_path"] == date_root / "site_a" / "diffs"


def test_changes_detected_false_lands_in_without_changes(tmp_path):
    date = "02-05-2026"
    date_root = tmp_path / date
    _seed_url_dir(date_root, "site_b", changes_detected=False)

    result = discover_comparison_data(tmp_path, date)

    assert len(result["with_changes"]) == 0
    assert len(result["without_changes"]) == 1
    entry = result["without_changes"][0]
    assert entry["url_name"] == "site_b"
    assert entry["has_changes"] is False


def test_only_top_level_changes_detected_is_consulted(tmp_path):
    """A.3 simplification: per-category flags are NOT consulted independently.

    Pre-A.3 code OR-ed 6 fields; A.3 trusts only `result.changes_detected`.
    This test pins that - a URL with `changes_detected=False` but per-category
    flags True (which would never happen in real comparator output, but is
    our regression guard) lands in without_changes. If someone reintroduces
    the OR, this test fails.
    """
    date = "02-05-2026"
    date_root = tmp_path / date
    _seed_url_dir(
        date_root,
        "trick",
        changes_detected=False,
        # All these per-category flags would have flipped the pre-A.3 OR check.
        extra_result={
            "screenshot": {"visual_changes": True},
            "dom": {"has_changes": True},
            "assets": {
                "css": {"has_changes": True},
                "js": {"has_changes": True},
                "media": {"has_changes": True},
            },
        },
    )
    result = discover_comparison_data(tmp_path, date)
    assert len(result["with_changes"]) == 0
    assert len(result["without_changes"]) == 1


def test_missing_changes_detected_field_treated_as_false(tmp_path):
    """Error-path comparator results have no `result.changes_detected` - they
    must land in without_changes (which is the pre-A.3 behavior preserved)."""
    date = "02-05-2026"
    date_root = tmp_path / date
    _seed_url_dir(
        date_root,
        "errored",
        changes_detected=None,
        extra_result={"error": "missing_baseline"},
    )

    result = discover_comparison_data(tmp_path, date)
    assert len(result["without_changes"]) == 1
    assert result["with_changes"] == []


# ---------------------------------------------------------------------------
# structured_data_path semantics
# ---------------------------------------------------------------------------


def test_structured_data_path_none_when_diffs_dir_missing(tmp_path):
    """If diffs/ doesn't exist, `structured_data_path` must be None - the
    report stage uses None as a sentinel that there's no per-category data
    to load (vs. an empty dict, which would break downstream)."""
    date = "02-05-2026"
    date_root = tmp_path / date
    _seed_url_dir(date_root, "no_diffs", changes_detected=True, create_diffs_dir=False)

    result = discover_comparison_data(tmp_path, date)
    assert result["with_changes"][0]["structured_data_path"] is None


def test_structured_data_path_set_when_diffs_dir_exists(tmp_path):
    date = "02-05-2026"
    date_root = tmp_path / date
    _seed_url_dir(date_root, "with_diffs", changes_detected=True, create_diffs_dir=True)

    result = discover_comparison_data(tmp_path, date)
    p = result["with_changes"][0]["structured_data_path"]
    assert p == date_root / "with_diffs" / "diffs"
    assert p.exists() and p.is_dir()


# ---------------------------------------------------------------------------
# Resilience to bad inputs - must skip, not raise
# ---------------------------------------------------------------------------


def test_skips_url_dirs_without_comparison_results_json(tmp_path, caplog):
    """A URL dir lacking comparison_results.json gets a warning, not an exception.

    Common when the comparator crashed mid-write or is still running. We don't
    want the report stage to crash because one URL is in a bad state.
    """
    date = "02-05-2026"
    date_root = tmp_path / date
    date_root.mkdir()
    (date_root / "incomplete").mkdir()  # url dir but no results file

    result = discover_comparison_data(tmp_path, date)
    assert result == {"with_changes": [], "without_changes": []}


def test_skips_malformed_json_and_logs_error(tmp_path):
    date = "02-05-2026"
    date_root = tmp_path / date
    _seed_url_dir(date_root, "broken", raw_json="{not valid json")
    _seed_url_dir(date_root, "good", changes_detected=True)

    result = discover_comparison_data(tmp_path, date)

    # The good one is still picked up; the broken one is silently skipped.
    assert [e["url_name"] for e in result["with_changes"]] == ["good"]
    assert result["without_changes"] == []


def test_ignores_non_directory_entries_in_date_dir(tmp_path):
    """Files at the date level (e.g. a stray .DS_Store) must not break iteration."""
    date = "02-05-2026"
    date_root = tmp_path / date
    date_root.mkdir()
    (date_root / ".DS_Store").write_text("not a dir")
    _seed_url_dir(date_root, "real_url", changes_detected=True)

    result = discover_comparison_data(tmp_path, date)
    assert len(result["with_changes"]) == 1
    assert result["with_changes"][0]["url_name"] == "real_url"


# ---------------------------------------------------------------------------
# Multi-URL bucketing
# ---------------------------------------------------------------------------


def test_buckets_mixed_urls_correctly(tmp_path):
    """Mixed input: some changed, some unchanged, one missing JSON, one malformed."""
    date = "02-05-2026"
    date_root = tmp_path / date

    _seed_url_dir(date_root, "changed_a", changes_detected=True)
    _seed_url_dir(date_root, "changed_b", changes_detected=True)
    _seed_url_dir(date_root, "unchanged_a", changes_detected=False)
    _seed_url_dir(date_root, "unchanged_b", changes_detected=False)
    _seed_url_dir(date_root, "broken", raw_json="garbage")
    # URL dir with no results file at all:
    (date_root / "missing_results").mkdir()

    result = discover_comparison_data(tmp_path, date)

    changed_names = sorted(e["url_name"] for e in result["with_changes"])
    unchanged_names = sorted(e["url_name"] for e in result["without_changes"])
    assert changed_names == ["changed_a", "changed_b"]
    assert unchanged_names == ["unchanged_a", "unchanged_b"]


def test_returned_entries_carry_full_comparison_data(tmp_path):
    """Each bucketed entry includes the parsed comparison_data dict - the
    report stage uses it for screenshot path resolution etc."""
    date = "02-05-2026"
    date_root = tmp_path / date
    _seed_url_dir(
        date_root,
        "site",
        changes_detected=True,
        extra_result={"screenshot": {"diff_image_path": "/tmp/diff.png"}},
    )

    entry = discover_comparison_data(tmp_path, date)["with_changes"][0]
    assert entry["comparison_data"]["result"]["changes_detected"] is True
    assert (
        entry["comparison_data"]["result"]["screenshot"]["diff_image_path"]
        == "/tmp/diff.png"
    )
    assert entry["comparison_data"]["metadata"]["url"] == "https://example.com/site"


# ---------------------------------------------------------------------------
# Phase B.1: drill-through to the latest comparator run dir
# ---------------------------------------------------------------------------


def test_drills_through_run_id_subdir_in_new_layout(tmp_path):
    """B.1: when the date dir contains run_id subdirs, discovery walks INTO
    the latest complete one rather than treating url_dirs as date-rooted.

    Verifies that the discovery logic doesn't accidentally iterate the
    date dir's children directly (which would now yield run_id dirs, not
    url_dirs, and produce nonsense results).
    """
    date = "02-05-2026"
    date_dir = tmp_path / date
    run_id = "01HXX0000000000000000000A0"
    run_dir = date_dir / run_id
    run_dir.mkdir(parents=True)

    # Manifest required so finder treats the run as complete.
    write_manifest(
        run_dir,
        Manifest(
            run_id=run_id,
            kind="comparator",
            started_at="01-01-2099 00:00:00",
            status="complete",
            finished_at="01-01-2099 00:00:01",
        ),
    )

    # url_dir nested under run_id, not under date.
    url_dir = run_dir / "site_a"
    url_dir.mkdir()
    (url_dir / "comparison_results.json").write_text(
        json.dumps(
            {"metadata": {"url": "https://x"}, "result": {"changes_detected": True}}
        ),
        encoding="utf-8",
    )

    result = discover_comparison_data(tmp_path, date)
    assert len(result["with_changes"]) == 1
    assert result["with_changes"][0]["url_name"] == "site_a"
    # The url_dir path should be inside the run_id dir, NOT the date dir.
    assert run_id in str(result["with_changes"][0]["url_dir"])


def test_skips_date_dir_when_only_running_runs_present(tmp_path):
    """B.1: a date dir whose only run is `status="running"` must yield empty
    buckets - discovery shouldn't accidentally fall back to legacy mode and
    treat the in-progress run dir as a url_dir."""
    date = "02-05-2026"
    date_dir = tmp_path / date
    run_id = "01HXX0000000000000000000A0"
    run_dir = date_dir / run_id
    run_dir.mkdir(parents=True)

    write_manifest(
        run_dir,
        Manifest(
            run_id=run_id,
            kind="comparator",
            started_at="01-01-2099 00:00:00",
            status="running",  # not yet complete
        ),
    )

    result = discover_comparison_data(tmp_path, date)
    assert result == {"with_changes": [], "without_changes": []}
