"""Comparator unit tests (Phase A.4).

Direct tests for the smaller-grained functions in `test_ui/comparator/`,
complementing the slow end-to-end golden in `test_comparator_golden.py`.

Marked `@pytest.mark.slow` because:
  - DOM tests import BeautifulSoup + lxml — fine, but bundled with the slow
    suite for one-stop run.
  - Screenshot tests use OpenCV (cv2) + skimage SSIM, which take 100-300ms
    per call and dominate cold-start cost. Pinned versions
    (opencv-python-headless==4.11.0.86, scikit-image==0.26.0) so byte-content
    of visual diffs stays stable; bumping either may shift outputs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from test_ui.comparator import dom, screenshots


pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# DOM diff — crafted HTML pairs
# ---------------------------------------------------------------------------


def _write_html(path: Path, html: str) -> Path:
    path.write_text(html, encoding="utf-8")
    return path


def test_compare_dom_returns_no_changes_for_identical_html(tmp_path):
    """Identical input → has_changes=False, content/length deltas zero."""
    html = "<html><head><title>T</title></head><body><p>same</p></body></html>"
    a = _write_html(tmp_path / "a.html", html)
    b = _write_html(tmp_path / "b.html", html)

    result = dom.compare_dom(a, b)

    assert "error" not in result
    assert result["has_changes"] is False
    assert result["title"]["changed"] is False
    assert result["content"]["length_change"] == 0
    assert result["structure"]["element_changes"] == []


def test_compare_dom_detects_title_change(tmp_path):
    a = _write_html(
        tmp_path / "a.html",
        "<html><head><title>Welcome</title></head><body><p>x</p></body></html>",
    )
    b = _write_html(
        tmp_path / "b.html",
        "<html><head><title>Welcome v2</title></head><body><p>x</p></body></html>",
    )

    result = dom.compare_dom(a, b)

    assert result["title"]["changed"] is True
    assert result["title"]["baseline"] == "Welcome"
    assert result["title"]["current"] == "Welcome v2"
    assert result["has_changes"] is True


def test_compare_dom_detects_added_element(tmp_path):
    a = _write_html(tmp_path / "a.html", "<html><body><div>one</div></body></html>")
    b = _write_html(
        tmp_path / "b.html", "<html><body><div>one</div><div>two</div></body></html>"
    )

    result = dom.compare_dom(a, b)

    div_changes = [
        c for c in result["structure"]["element_changes"] if c["element"] == "div"
    ]
    assert len(div_changes) == 1
    assert div_changes[0]["change_type"] == "added"
    assert div_changes[0]["count_change"] == 1


def test_compare_dom_detects_removed_high_impact_element(tmp_path):
    """High-impact tags (form/button/input) currently get impact='high' on
    BOTH addition and removal — the `change_type` parameter to
    `assess_element_impact` is unused (see latent-bug pin in
    test_assess_element_impact_high_impact_branch_is_unconditional below)."""
    a = _write_html(
        tmp_path / "a.html", "<html><body><button>Submit</button><p>x</p></body></html>"
    )
    b = _write_html(tmp_path / "b.html", "<html><body><p>x</p></body></html>")

    result = dom.compare_dom(a, b)
    btn = [
        c for c in result["structure"]["element_changes"] if c["element"] == "button"
    ]
    assert len(btn) == 1
    assert btn[0]["change_type"] == "removed"
    assert btn[0]["impact"] == "high"  # pinned: same as 'added' (see flag)


def test_compare_dom_significant_content_change_threshold(tmp_path):
    """`content.significant_change` is True iff abs(length_change) > 100.

    The threshold is currently 100 (see dom.compare_dom). Pinning it because
    the comparator-summary content-block bug fix (A.3) made this flag visible
    in the change_summary.json output for the first time.
    """
    a = _write_html(tmp_path / "a.html", "<html><body><p>short</p></body></html>")
    # Body text is 200 chars longer.
    long_text = "x" * 200
    b = _write_html(
        tmp_path / "b.html", f"<html><body><p>{long_text}</p></body></html>"
    )

    result = dom.compare_dom(a, b)
    assert result["content"]["significant_change"] is True
    assert result["content"]["length_change"] >= 100


def test_compare_dom_below_significance_threshold(tmp_path):
    """A small content delta must NOT trip significant_change."""
    a = _write_html(tmp_path / "a.html", "<html><body><p>aaa</p></body></html>")
    b = _write_html(tmp_path / "b.html", "<html><body><p>aaab</p></body></html>")

    result = dom.compare_dom(a, b)
    assert result["content"]["significant_change"] is False


def test_compare_dom_meta_changes_detected(tmp_path):
    a = _write_html(
        tmp_path / "a.html",
        '<html><head><meta name="description" content="Old"></head><body/></html>',
    )
    b = _write_html(
        tmp_path / "b.html",
        '<html><head><meta name="description" content="New"></head><body/></html>',
    )

    result = dom.compare_dom(a, b)
    assert result["meta"]["changes"], "meta description change should appear"
    assert result["has_changes"] is True


def test_compare_dom_returns_error_for_missing_files(tmp_path):
    """Missing files → typed error dict, no exception."""
    result = dom.compare_dom(tmp_path / "nope_a.html", tmp_path / "nope_b.html")
    assert "error" in result
    assert result["error"] == "HTML files missing"


def test_compare_dom_handles_malformed_html_gracefully(tmp_path):
    """BeautifulSoup is forgiving; we should still return a sensible result, not crash."""
    a = _write_html(tmp_path / "a.html", "<html><body><p>")  # unclosed tags
    b = _write_html(tmp_path / "b.html", "<not-real><<<>>>")
    result = dom.compare_dom(a, b)
    # bs4 with lxml parses these without raising — assert we got a dict back,
    # not an error envelope.
    assert "error" not in result
    assert "has_changes" in result


# ---------------------------------------------------------------------------
# create_html_changes_json — projection of dom_result into AI-facing shape
# ---------------------------------------------------------------------------


def test_create_html_changes_json_passes_through_error(tmp_path):
    """Error envelope from compare_dom must produce a degraded but valid summary."""
    error_input = {"error": "DOM comparison failed: ..."}
    out = dom.create_html_changes_json(error_input)

    assert out["changes_detected"] is False
    assert out["changes"] == []
    assert out["summary"]["severity"] == "none"


def test_create_html_changes_json_emits_title_change_record(tmp_path):
    a = _write_html(
        tmp_path / "a.html", "<html><head><title>A</title></head><body/></html>"
    )
    b = _write_html(
        tmp_path / "b.html", "<html><head><title>B</title></head><body/></html>"
    )
    result = dom.compare_dom(a, b)
    out = dom.create_html_changes_json(result)

    title_changes = [c for c in out["changes"] if c["element"] == "title"]
    assert len(title_changes) == 1
    assert title_changes[0]["old_value"] == "A"
    assert title_changes[0]["new_value"] == "B"


# ---------------------------------------------------------------------------
# assess_element_impact — pure heuristic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag,count_diff,expected",
    [
        # HIGH_IMPACT_TAGS — always 'high', no magnitude threshold.
        ("form", 1, "high"),
        ("button", 1, "high"),
        ("input", 5, "high"),
        # MEDIUM_IMPACT_TAGS: 'medium' if count_diff > 2 else 'low'.
        ("a", 3, "medium"),
        ("img", 1, "low"),
        ("h1", 2, "low"),
        # LOW_IMPACT_TAGS: 'low' if count_diff < 10 else 'medium'.
        ("div", 5, "low"),
        ("span", 50, "medium"),
        # Tag outside the three classes — always 'low'.
        ("blockquote", 100, "low"),
    ],
)
def test_assess_element_impact(tag, count_diff, expected):
    assert dom.assess_element_impact(tag, count_diff) == expected


def test_assess_element_impact_high_impact_is_unconditional():
    """HIGH_IMPACT_TAGS always rate 'high' — no magnitude threshold.

    Pre-cleanup the function had a `count_diff > 0 else 'medium'` ternary
    in the HIGH_IMPACT branch, but `count_diff` is always positive at the
    call sites (`abs(current - baseline)`, only invoked when those differ),
    so the 'medium' arm was dead code. Cleanup dropped it; this test pins
    the unconditional 'high' so a future maintainer reintroducing
    asymmetric scoring (e.g. medium-on-remove, high-on-add) makes the
    decision deliberately rather than slipping it back in.
    """
    assert dom.assess_element_impact("form", 1) == "high"
    assert dom.assess_element_impact("form", 99) == "high"
    assert dom.assess_element_impact("button", 1) == "high"


# ---------------------------------------------------------------------------
# Screenshots — synthetic Pillow images
# ---------------------------------------------------------------------------


def _make_solid_png(path: Path, size=(100, 100), color=(255, 0, 0)) -> Path:
    Image.new("RGB", size, color).save(path)
    return path


def test_compare_screenshots_identical_images_perfect_ssim(tmp_path):
    """Identical PNGs → SSIM == 1.0, no diff image written."""
    a = _make_solid_png(tmp_path / "a.png", color=(0, 255, 0))
    b = _make_solid_png(tmp_path / "b.png", color=(0, 255, 0))
    diffs_dir = tmp_path / "diffs"

    result = screenshots.compare_screenshots(a, b, "https://test/", diffs_dir)

    assert result["ssim_score"] == pytest.approx(1.0, abs=1e-6)
    assert result["visual_changes"] is False
    # Identical means no diff image needed.
    assert not (diffs_dir / "visual_diff.png").exists()


def test_compare_screenshots_different_colors_low_ssim(tmp_path):
    """Solid red vs solid blue — drastically different. SSIM should be low,
    visual_diff.png written."""
    a = _make_solid_png(tmp_path / "a.png", color=(255, 0, 0))
    b = _make_solid_png(tmp_path / "b.png", color=(0, 0, 255))
    diffs_dir = tmp_path / "diffs"

    result = screenshots.compare_screenshots(a, b, "https://test/", diffs_dir)

    assert result["ssim_score"] < 0.999, (
        "drastically different colors should not be near-identical"
    )
    assert result["visual_changes"] is True
    assert (diffs_dir / "visual_diff.png").exists()
    # Diff PNG should have non-trivial size (Otsu mask + contour drawing).
    assert (diffs_dir / "visual_diff.png").stat().st_size > 100


def test_compare_screenshots_dimension_change_flagged(tmp_path):
    """Different image dimensions must set dimensions_changed=True.

    The function still resizes to the larger dimension to produce an SSIM,
    but the dimension-change flag tells the report renderer to display a
    layout-shift warning.
    """
    a = _make_solid_png(tmp_path / "a.png", size=(100, 100), color=(255, 0, 0))
    b = _make_solid_png(tmp_path / "b.png", size=(150, 100), color=(255, 0, 0))
    diffs_dir = tmp_path / "diffs"

    result = screenshots.compare_screenshots(a, b, "https://test/", diffs_dir)

    assert result["dimensions_changed"] is True


def test_compare_screenshots_returns_error_for_missing_files(tmp_path):
    """Missing screenshots → error dict with ssim_score=0.0 (caller's contract)."""
    result = screenshots.compare_screenshots(
        tmp_path / "nope_a.png",
        tmp_path / "nope_b.png",
        "https://test/",
        tmp_path / "diffs",
    )
    assert "error" in result
    assert result["ssim_score"] == 0.0


def test_compare_screenshots_does_not_create_diffs_dir_when_identical(tmp_path):
    """Identical images must NOT create a diffs/ directory.

    Pre-A.3 generator did `diffs_dir.mkdir(exist_ok=True)` unconditionally;
    we want empty diffs/ dirs to never appear so the report layer can use
    "diffs/ exists" as a meaningful signal.
    """
    a = _make_solid_png(tmp_path / "a.png", color=(0, 255, 0))
    b = _make_solid_png(tmp_path / "b.png", color=(0, 255, 0))
    diffs_dir = tmp_path / "diffs"

    screenshots.compare_screenshots(a, b, "https://test/", diffs_dir)

    assert not diffs_dir.exists(), "diffs/ should not be created for identical inputs"


def test_compare_screenshots_writes_diff_image_for_subtle_change(tmp_path):
    """Slight color shift in a region — SSIM < 1.0, diff image written.

    Uses Pillow to paint a red square onto a green canvas in the 'current'
    image only. The diff image should highlight the changed region.
    """
    a_img = Image.new("RGB", (100, 100), (0, 255, 0))
    a_img.save(tmp_path / "a.png")

    b_img = Image.new("RGB", (100, 100), (0, 255, 0))
    # Paint a red square in the top-left 30x30 region.
    for x in range(30):
        for y in range(30):
            b_img.putpixel((x, y), (255, 0, 0))
    b_img.save(tmp_path / "b.png")

    diffs_dir = tmp_path / "diffs"
    result = screenshots.compare_screenshots(
        tmp_path / "a.png",
        tmp_path / "b.png",
        "https://test/",
        diffs_dir,
    )

    assert result["visual_changes"] is True
    assert 0.0 < result["ssim_score"] < 1.0
    assert (diffs_dir / "visual_diff.png").exists()
