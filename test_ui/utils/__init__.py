"""Deprecated shim - moved to test_ui.common.images in Phase A.3.

Kept as a re-export so any external code importing from `test_ui.utils`
doesn't silently break. Remove on the next major version bump.
"""

from ..common.images import compress_base64_screenshot

__all__ = ["compress_base64_screenshot"]
