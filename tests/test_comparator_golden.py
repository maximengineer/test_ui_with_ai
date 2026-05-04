"""Comparator golden test (Phase A.2 - slow-marked).

Drives `ComparatorEngine.compare_all` against a synthetic baseline/current
directory pair laid out on disk. Pins the diff JSON outputs against goldens.

What's checked:
  - `diffs/html_changes.json` - byte-equal (after normalizing volatile fields)
  - `diffs/change_summary.json` - SSIM-tolerant compare (float fields → approx)
  - `diffs/visual_diff.png` - existence + size-bounds only (binary content
    drifts with OpenCV/skimage versions; pinned versions in pyproject.toml
    keep this stable but we don't byte-compare to be safe)

What's NOT checked: `comparison_results.json` (per-tmp-dir paths in metadata
make it noisy; the diff JSONs are the AI-facing surface and worth pinning).

Marked `@pytest.mark.slow` because importing cv2/skimage and running SSIM
costs a few hundred ms - the fast suite skips it by default. Run with
`pytest -m slow`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from test_ui.comparator.engine import ComparatorEngine
from test_ui.comparator.finder import find_latest_run_dir_in_date
from test_ui.config import settings


pytestmark = pytest.mark.slow


def _find_diffs_dir(setup) -> Path:
    """Resolve the post-run diffs directory under the new B.1 layout.

    Drills through `<output>/<date>/<run_id>/<url_dir>/diffs/`. The test
    fixture creates only one date dir, but compare_all publishes a fresh
    ULID `run_id` under it; `find_latest_run_dir_in_date` resolves that.
    """
    date_dirs = list(setup["output"].iterdir())
    assert len(date_dirs) == 1, f"expected 1 date dir, got {date_dirs}"
    run_root = find_latest_run_dir_in_date(date_dirs[0])
    assert run_root is not None, f"no complete run published in {date_dirs[0]}"
    return run_root / setup["url_dir_name"] / "diffs"


def _seed_url_dir(
    parent: Path, url_dir_name: str, html: str, color: tuple[int, int, int]
) -> None:
    """Lay down minimal index.html + screenshot.png + empty asset subdirs."""
    url_dir = parent / url_dir_name
    url_dir.mkdir(parents=True, exist_ok=True)
    (url_dir / "index.html").write_text(html, encoding="utf-8")
    Image.new("RGB", (200, 200), color).save(url_dir / "screenshot.png")
    # comparator's assets.compare_assets walks css/, js/, media/ subdirs; create empty
    for asset in ("css", "js", "media"):
        (url_dir / asset).mkdir(exist_ok=True)


@pytest.fixture
def comparator_test_setup(tmp_path, monkeypatch):
    """Lay out a baseline + current pair under tmp; monkeypatch settings.comparator_dir."""
    baseline_dir = tmp_path / "baseline"
    current_dir = tmp_path / "current"
    output_dir = tmp_path / "comparator_output"
    output_dir.mkdir()
    monkeypatch.setattr(settings, "comparator_dir", output_dir)

    url_dir_name = "test.example_demo"
    baseline_html = (
        "<!DOCTYPE html><html><head><title>Welcome</title>"
        '<meta name="description" content="Original page">'
        "</head><body>"
        "<h1>Hello</h1><p>Original paragraph text.</p>"
        "<div>One</div><div>Two</div>"
        "</body></html>"
    )
    current_html = (
        "<!DOCTYPE html><html><head><title>Welcome v2</title>"
        '<meta name="description" content="Updated page">'
        "</head><body>"
        "<h1>Hello</h1><p>Updated paragraph with more text content here.</p>"
        "<div>One</div><div>Two</div><div>Three</div>"
        "</body></html>"
    )
    _seed_url_dir(baseline_dir, url_dir_name, baseline_html, (255, 0, 0))  # red
    _seed_url_dir(current_dir, url_dir_name, current_html, (255, 100, 100))  # pinkish

    return {
        "baseline": baseline_dir,
        "current": current_dir,
        "output": output_dir,
        "url": "https://test.example/demo",
        "url_dir_name": url_dir_name,
    }


def _normalize_floats_for_compare(obj, *, rel: float = 1e-2):
    """Round floats to a tolerance so SSIM jitter doesn't break compare."""
    if isinstance(obj, dict):
        return {k: _normalize_floats_for_compare(v, rel=rel) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_floats_for_compare(v, rel=rel) for v in obj]
    if isinstance(obj, float):
        # Coarsely round so per-build SSIM jitter doesn't show in the diff.
        return round(obj, 2)
    return obj


def test_comparator_golden_html_changes(comparator_test_setup, golden_compare):
    """Pin diffs/html_changes.json content for the synthetic before/after pair."""
    setup = comparator_test_setup
    engine = ComparatorEngine()
    engine.compare_all(setup["baseline"], setup["current"], [{"url": setup["url"]}])

    diffs_dir = _find_diffs_dir(setup)
    assert diffs_dir.exists(), "diffs/ not created"

    html_changes = json.loads((diffs_dir / "html_changes.json").read_text())
    # html_changes records carry deterministic data (no timestamps, no UUIDs)
    # so a straight golden compare works.
    golden_compare(html_changes, "comparator_html_changes.json", subdir="comparator")


def test_comparator_golden_change_summary(comparator_test_setup, golden_compare):
    """Pin diffs/change_summary.json - float SSIM fields rounded for stability;
    `affected_components` sorted because the producer uses `list(set(...))`
    which has non-deterministic iteration order. Flagged for A.3 to fix the
    producer side."""
    setup = comparator_test_setup
    engine = ComparatorEngine()
    engine.compare_all(setup["baseline"], setup["current"], [{"url": setup["url"]}])

    diffs_dir = _find_diffs_dir(setup)
    summary = json.loads((diffs_dir / "change_summary.json").read_text())
    summary = _normalize_floats_for_compare(summary)
    if isinstance(summary.get("affected_components"), list):
        summary["affected_components"] = sorted(summary["affected_components"])
    golden_compare(summary, "comparator_change_summary.json", subdir="comparator")


def test_comparator_visual_diff_exists_and_reasonable_size(comparator_test_setup):
    """visual_diff.png - existence + 1KB-1MB range, not byte-equality."""
    setup = comparator_test_setup
    engine = ComparatorEngine()
    engine.compare_all(setup["baseline"], setup["current"], [{"url": setup["url"]}])

    visual_diff = _find_diffs_dir(setup) / "visual_diff.png"
    assert visual_diff.exists(), "visual_diff.png not generated"
    size = visual_diff.stat().st_size
    # 200x200 RGB PNG with diff highlights: should be in this range. Loose
    # bounds because libpng + cv2 versions affect compression slightly.
    assert 500 < size < 1_000_000, f"visual_diff.png size {size} outside expected range"


def test_comparator_other_diff_files_exist(comparator_test_setup):
    """css_changes.json + js_changes.json must always be written too."""
    setup = comparator_test_setup
    engine = ComparatorEngine()
    engine.compare_all(setup["baseline"], setup["current"], [{"url": setup["url"]}])

    diffs_dir = _find_diffs_dir(setup)
    for fname in (
        "css_changes.json",
        "js_changes.json",
        "change_summary.json",
        "html_changes.json",
    ):
        assert (diffs_dir / fname).exists(), f"missing {fname}"


# ---------------------------------------------------------------------------
# Phase B.3: per-site dir naming derives from `site["id"]`, not the URL.
# The other golden tests above pass `[{"url": ...}]` (no id) which exercises
# the legacy `url_to_dirname` fallback. This test pins the NEW path -
# fixture seeds dirs by id, comparator looks them up by id.
# ---------------------------------------------------------------------------


@pytest.fixture
def comparator_id_setup(tmp_path, monkeypatch):
    """Like comparator_test_setup but seeds by site id, not url-derived name."""
    baseline_dir = tmp_path / "baseline"
    current_dir = tmp_path / "current"
    output_dir = tmp_path / "comparator_output"
    output_dir.mkdir()
    monkeypatch.setattr(settings, "comparator_dir", output_dir)

    site_id = "homepage-prod"  # explicit id - the post-B.3 contract
    url = "https://demo.example.com/about/"  # url has nothing to do with the dir name
    baseline_html = (
        "<html><head><title>X</title></head><body><p>before</p></body></html>"
    )
    current_html = "<html><head><title>Y</title></head><body><p>after</p></body></html>"

    _seed_url_dir(baseline_dir, site_id, baseline_html, (255, 0, 0))
    _seed_url_dir(current_dir, site_id, current_html, (255, 100, 100))

    return {
        "baseline": baseline_dir,
        "current": current_dir,
        "output": output_dir,
        "site": {"id": site_id, "name": "Homepage Prod", "url": url},
        "site_id": site_id,
    }


def test_comparator_uses_site_id_for_dir_lookup(comparator_id_setup):
    """B.3 contract: when a site dict carries `id`, the comparator looks for
    `<baseline>/<site.id>/` regardless of the URL. Pinning this catches a
    regression where someone reverts the lookup to `url_to_dirname(url)`
    - that would yield 'demo.example.com_about' here, NOT find the seeded
    'homepage-prod' dir, and report missing_baseline for every site."""
    setup = comparator_id_setup
    engine = ComparatorEngine()
    results = engine.compare_all(setup["baseline"], setup["current"], [setup["site"]])

    assert len(results) == 1
    result = results[0]["result"]
    assert "error" not in result, f"got {result}; comparator failed to find by id"
    assert results[0]["metadata"]["site_id"] == setup["site_id"]
    # And the published run dir contains a subdir named by the site id, not by the URL.
    diffs_dir = (
        find_latest_run_dir_in_date(next(setup["output"].iterdir()))
        / setup["site_id"]
        / "diffs"
    )
    assert diffs_dir.exists(), f"no diffs/ found at {diffs_dir} - id-based dir not used"
