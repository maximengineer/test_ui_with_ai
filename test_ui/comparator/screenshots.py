"""Screenshot comparison: SSIM scoring + visual-diff image generation.

Phase A.3 split — extracted from comparator/engine.py. **Hard import** of
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

        if score < 1.0:
            diffs_dir.mkdir(exist_ok=True)
            diff_image_path = diffs_dir / "visual_diff.png"
            _write_visual_diff(diff, current_resized, diff_image_path)
            return {
                "ssim_score": float(score),
                "diff_image_path": str(diff_image_path.absolute()),
                "dimensions_changed": dimensions_changed,
                "visual_changes": True,
            }
        return {
            "ssim_score": float(score),
            "dimensions_changed": dimensions_changed,
            "visual_changes": False,
        }
    except Exception as e:
        logger.error(f"Error comparing screenshots for {url}: {e}")
        return {"error": f"Screenshot comparison failed: {e!s}", "ssim_score": 0.0}


def _write_visual_diff(
    diff: np.ndarray, current_resized: np.ndarray, out_path: Path
) -> None:
    """Highlight regions that differ on a copy of the current screenshot.

    Otsu-thresholds the SSIM diff to a binary mask, finds external contours,
    draws red rectangles around each on the current frame. Output written to
    `out_path`. Compression artifacts can produce noisy small contours — the
    A.2 visual-diff golden test only checks size-bounds, not byte equality,
    to absorb that.
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
    contours = contours[0] if len(contours) == 2 else contours[1]

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
