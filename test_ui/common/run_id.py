"""ULID-based run identity (Phase B.1.1).

A `run_id` is the primary key of every pipeline execution. Two crawls on the
same date no longer collide because each gets its own run_id directory under
`data/<kind>/DD-MM-YYYY/<run_id>/`.

Why ULID over UUID4: ULIDs are sortable by creation time (the first 48 bits
are a millisecond timestamp), so a `sorted(os.listdir(date_dir))` puts runs
in chronological order without needing to read manifests. They're also
URL-safe (Crockford base32, no dashes) and shorter than UUIDs (26 vs 36
chars) - easier to copy-paste.

The `python-ulid` lib's `ULID()` constructor uses os.urandom for entropy
so collisions within the same millisecond are statistically impossible
(80 bits of randomness). We don't need monotonic generation.
"""

from __future__ import annotations

from ulid import ULID


def new_run_id() -> str:
    """Return a fresh ULID as a 26-char Crockford-base32 string.

    Sortable lexicographically by generation time. Safe to use as a
    directory name on every supported filesystem.
    """
    return str(ULID())


def is_valid_run_id(s: str) -> bool:
    """True iff `s` parses as a ULID.

    Used by finder.py to distinguish run_id directories from other entries
    (e.g. the `latest` symlink, or a stray .DS_Store) when scanning a date
    directory in legacy-fallback mode.
    """
    try:
        ULID.from_str(s)
    except (ValueError, TypeError):
        return False
    return True


__all__ = ["new_run_id", "is_valid_run_id"]
