"""Screenshot comparison: SSIM scoring + visual-diff image generation.

Phase A.3 split - extracted from comparator/engine.py. **Hard import** of
cv2 + skimage per plan; the prior code had a `CV2_AVAILABLE` flag that
would silently degrade if the libs were missing. Hard import = fail at
startup, not silently produce zero-information results.

Pinned to opencv-python-headless ==4.11.0.86 + scikit-image ==0.26.0
(see pyproject.toml). Bumping either may shift SSIM scores or visual_diff.png
byte content; regenerate Phase A.2 goldens with `pytest --update-golden -m slow`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
from loguru import logger
from PIL import Image
from skimage.metrics import structural_similarity as ssim

from ..config import settings


def compare_screenshots(
    baseline_img_path: Path,
    current_img_path: Path,
    url: str,
    diffs_dir: Path,
) -> dict[str, Any]:
    """Compare two screenshots; return an SSIM-based result dict.

    On any failure, returns `{"error": "...", "ssim_score": 0.0}` so the
    caller's downstream change-summary code keeps working without checking
    every field. This is intentional: missing screenshots, decode failures,
    etc. all collapse to "couldn't compare" rather than crashing the run.
    """
    if not baseline_img_path.exists() or not current_img_path.exists():
        return {"error": "Screenshots missing", "ssim_score": 0.0}

    try:
        baseline_img = _load_image_robust(baseline_img_path)
        current_img = _load_image_robust(current_img_path)

        if baseline_img is None or current_img is None:
            return {"error": "Could not load screenshots", "ssim_score": 0.0}

        dimensions_changed = baseline_img.shape != current_img.shape

        # Resize both to the larger dimensions so SSIM has a comparable basis.
        height = max(baseline_img.shape[0], current_img.shape[0])
        width = max(baseline_img.shape[1], current_img.shape[1])
        baseline_resized = cv2.resize(baseline_img, (width, height))
        current_resized = cv2.resize(current_img, (width, height))

        baseline_gray = cv2.cvtColor(baseline_resized, cv2.COLOR_BGR2GRAY)
        current_gray = cv2.cvtColor(current_resized, cv2.COLOR_BGR2GRAY)

        score, diff = ssim(baseline_gray, current_gray, full=True)

        # Two complementary signals decide `visual_changes`:
        #
        # 1. SSIM mean threshold (`visual_similarity_threshold`, default
        #    0.95) catches large-area changes and uniform luminance
        #    shifts. Above-threshold = no overall change beyond
        #    encoding noise.
        #
        # 2. Max-contour-area gate (`visual_min_contour_area`, default
        #    50 px²) catches LOCALIZED changes that don't move the SSIM
        #    mean enough to trip threshold #1. An 80x80 painted rect
        #    on a 1080x600 image is SSIM ~0.99 (above 0.95) but
        #    produces a single 6400 px² contour - clearly a real change
        #    that #1 alone would miss (validated against the
        #    `tamper_baseline.py` site-1 visual:drastic case).
        #
        # SSIM also stays in the response payload as a continuous
        # signal for the AI severity rollup; the binary gate is just
        # whether ANY of the two signals trips.
        contours = _find_diff_contours(diff)
        max_area = _max_contour_area(contours)
        ssim_changed = score < settings.visual_similarity_threshold
        contour_changed = max_area >= settings.visual_min_contour_area

        if ssim_changed or contour_changed:
            diffs_dir.mkdir(exist_ok=True)
            diff_image_path = diffs_dir / "visual_diff.png"
            _write_visual_diff_from_contours(contours, current_resized, diff_image_path)
            return {
                "ssim_score": float(score),
                "max_contour_area": int(max_area),
                "diff_image_path": str(diff_image_path.absolute()),
                "dimensions_changed": dimensions_changed,
                "visual_changes": True,
            }
        return {
            "ssim_score": float(score),
            "max_contour_area": int(max_area),
            "dimensions_changed": dimensions_changed,
            "visual_changes": False,
        }
    except Exception as e:
        logger.error(f"Error comparing screenshots for {url}: {e}")
        return {"error": f"Screenshot comparison failed: {e!s}", "ssim_score": 0.0}


def _find_diff_contours(diff: np.ndarray) -> list:
    """Otsu-threshold the SSIM diff image, return external contours.

    Extracted from `_write_visual_diff` so the contour-area gate in
    `compare_screenshots` can use the contour count/area as a binary
    signal without re-running the contour pass when actually writing
    the diff PNG.
    """
    diff_normalized = (diff * 255).astype("uint8")
    thresh = cv2.threshold(
        diff_normalized,
        0,
        255,
        cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU,
    )[1]
    contours = cv2.findContours(
        thresh.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    # OpenCV 3.x returned (img, contours, hierarchy); 4.x returns
    # (contours, hierarchy). Tolerate either via length check, so a
    # cv2 minor-version bump doesn't silently break detection.
    return list(contours[0] if len(contours) == 2 else contours[1])


def _max_contour_area(contours: list) -> int:
    """Largest contour area in px². 0 when contours is empty."""
    if not contours:
        return 0
    return max(int(cv2.contourArea(c)) for c in contours)


def _write_visual_diff_from_contours(
    contours: list, current_resized: np.ndarray, out_path: Path
) -> None:
    """Draw red bounding rectangles around `contours` on a copy of the
    current screenshot and write to `out_path`. Compression artifacts
    can produce noisy small contours - the A.2 visual-diff golden test
    only checks size-bounds, not byte equality, to absorb that.
    """
    diff_visual = current_resized.copy()
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        cv2.rectangle(diff_visual, (x, y), (x + w, y + h), (0, 0, 255), 2)
    cv2.imwrite(str(out_path), diff_visual)


def _load_image_robust(img_path: Path) -> np.ndarray | None:
    """Load PNG/JPEG via OpenCV; fall back to Pillow for other formats (e.g. WebP)."""
    try:
        img = cv2.imread(str(img_path))
        if img is not None:
            return img
        # OpenCV doesn't always do WebP; Pillow does.
        pil_img = Image.open(img_path)
        if pil_img.mode != "RGB":
            pil_img = pil_img.convert("RGB")
        img_array = np.array(pil_img)
        return cv2.cvtColor(img_array, cv2.COLOR_RGB2BGR)
    except Exception as e:
        logger.error(f"Failed to load image {img_path}: {e}")
        return None


__all__ = ["compare_screenshots"]
