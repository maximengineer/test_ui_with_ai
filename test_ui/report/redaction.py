"""Structured-data redaction before AI analysis.

This masks obvious secrets in comparator text fields without changing the AI
request schema. It is deliberately conservative: it targets high-confidence
tokens and leaves ordinary URLs/selectors/code structure intact so the analyzer
can still reason about what changed.
"""

from __future__ import annotations

import re
from typing import Any


_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
        "Bearer [REDACTED]",
    ),
    (
        re.compile(r"\bBasic\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
        "Basic [REDACTED]",
    ),
    (
        re.compile(
            r"\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|secret)"
            r"(\s*[:=]\s*)"
            r"([\"'])"
            r"([^\"']+)"
            r"\3",
            re.IGNORECASE,
        ),
        r"\1\2\3[REDACTED]\3",
    ),
    (
        re.compile(
            r"\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|secret)"
            r"(\s*[:=]\s*|%3[dD])"
            r"([^\s'\"&;,)<>{}]+)",
            re.IGNORECASE,
        ),
        r"\1\2[REDACTED]",
    ),
    (
        re.compile(
            r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"
        ),
        "[REDACTED_JWT]",
    ),
    (
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
        "[REDACTED_EMAIL]",
    ),
)


def redact_structured_data(value: Any) -> Any:
    """Recursively redact likely secrets from JSON-like structured data."""
    if isinstance(value, dict):
        return {k: redact_structured_data(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_structured_data(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(value: str) -> str:
    """Mask high-confidence secrets in a single string."""
    redacted = value
    for pattern, replacement in _REDACTION_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


__all__ = ["redact_structured_data", "redact_text"]
