"""
Simple image compression utilities for screenshot optimization.
Keeps screenshots under 5MB while maintaining quality for AI analysis.
"""

import base64
import io
import logging
from pathlib import Path
from typing import Tuple
from PIL import Image, ImageFile

# Enable loading of truncated images
ImageFile.LOAD_TRUNCATED_IMAGES = True

logger = logging.getLogger(__name__)


def compress_base64_screenshot(
    base64_data: str, output_path: Path
) -> Tuple[bool, str, int]:
    """
    Compress a base64-encoded screenshot using optimal format for AI analysis.

    Args:
        base64_data: Base64-encoded image data (with or without data URL prefix)
        output_path: Path for output file

    Returns:
        Tuple of (success, message, final_file_size)
    """
    try:
        # Remove data URL prefix if present
        if base64_data.startswith("data:image/"):
            base64_data = base64_data.split(",", 1)[1]

        # Decode base64 to bytes
        image_bytes = base64.b64decode(base64_data)

        # Load with PIL
        pil_image = Image.open(io.BytesIO(image_bytes))
        if pil_image.mode != "RGB":
            pil_image = pil_image.convert("RGB")

        # Get original size estimate
        buffer = io.BytesIO()
        pil_image.save(buffer, format="PNG")
        original_size = len(buffer.getvalue())

        # Try compression methods in order of preference for AI analysis:
        # 1. WebP 90% - Best balance of size/quality for screenshots
        # 2. JPEG 95% - Excellent quality, very small size
        # 3. PNG optimized - Fallback for perfect quality

        success, compressed_data, format_used = _try_compression_methods(
            pil_image, original_size
        )

        if not success:
            return False, "All compression methods failed", 0

        # Save compressed image
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(compressed_data)

        final_size = len(compressed_data)
        reduction_percent = ((original_size - final_size) / original_size) * 100

        message = (
            f"Compressed using {format_used} - "
            f"Size: {final_size / (1024 * 1024):.2f}MB "
            f"({reduction_percent:.1f}% reduction)"
        )

        return True, message, final_size

    except Exception as e:
        return False, f"Compression error: {str(e)}", 0


def _try_compression_methods(
    pil_image: Image.Image, original_size: int
) -> Tuple[bool, bytes, str]:
    """Try the 3 most useful compression methods for AI analysis."""
    MAX_SIZE = 4.5 * 1024 * 1024  # 4.5MB limit

    # Method 1: WebP 90% - Best for AI analysis (great quality, small size)
    try:
        buffer = io.BytesIO()
        pil_image.save(buffer, format="WEBP", quality=90, method=6)
        compressed_data = buffer.getvalue()
        if len(compressed_data) <= MAX_SIZE:
            return True, compressed_data, "WebP (quality=90)"
    except Exception:
        pass

    # Method 2: JPEG 95% - Excellent quality, very compact
    try:
        buffer = io.BytesIO()
        pil_image.save(buffer, format="JPEG", quality=95, optimize=True)
        compressed_data = buffer.getvalue()
        if len(compressed_data) <= MAX_SIZE:
            return True, compressed_data, "JPEG (quality=95)"
    except Exception:
        pass

    # Method 3: PNG optimized - Fallback for lossless quality
    try:
        buffer = io.BytesIO()
        pil_image.save(buffer, format="PNG", optimize=True, compress_level=9)
        compressed_data = buffer.getvalue()
        if len(compressed_data) <= MAX_SIZE:
            return True, compressed_data, "PNG (optimized)"
    except Exception:
        pass

    return False, b"", "No suitable compression found"
