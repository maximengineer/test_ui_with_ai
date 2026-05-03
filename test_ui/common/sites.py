"""Site list loader + `Site` model (Phase B.3.1).

A site is `{id, name, url}` where `id` is the **stable identifier** the rest
of the pipeline uses for per-site directory names (e.g.
`data/baseline/<date>/<run_id>/<id>/`). Naming the data dirs by `id` instead
of by URL means a URL change (e.g. site moves from `/about` to `/about-us`)
keeps history continuous — same id, same data path, comparator can still
diff baseline vs. current.

`name` is human-facing display text. It can change freely without touching
on-disk identifiers.

**Loader behaviour for backward compatibility (pre-B.3 sites.yml):**

  - If `id` is missing in YAML, we synthesize one from `name` (slugified +
    deduplicated against other sites in the same file) and emit a WARNING
    pointing the operator at `scripts/migrate_sites_ids.py` to write the
    ids back to disk.
  - If `name` is also missing or empty, we fall back to slugifying the URL
    via `url_to_dirname` so the loader never raises on legacy data — but
    the warning is more strident.
  - The Pydantic model itself REQUIRES `id` and rejects unknown fields, so
    drift in either direction (typo, dropped field) is loud.

`load_sites(path)` returns `list[Site]`. Existing callers that expect
`list[dict]` get the same shape via `[s.model_dump() for s in load_sites(...)]`.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from .url_id import url_to_dirname


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Lowercase + replace non-alphanumeric with `-` + collapse + strip.

    Stable, deterministic, ASCII-only. Doesn't try to romanize Unicode —
    operators with non-ASCII names should set `id:` explicitly.
    """
    out = _SLUG_RE.sub("-", value.lower()).strip("-")
    return out or "site"  # never return empty


class Site(BaseModel):
    """A single entry in sites.yml. `id` is the stable filesystem-safe key."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str
    url: str


def dedupe_slug(candidate: str, taken: set[str]) -> str:
    """Append `-2`, `-3`, ... until the slug is unique relative to `taken`.

    The single source of truth for the suffix scheme. `load_sites` uses it
    to auto-generate ids at runtime; `scripts/migrate_sites_ids.py` uses
    the same function so the loader and the migration script produce
    identical ids for the same input — without a shared helper they would
    silently desync if either side changed the suffix style.
    """
    if candidate not in taken:
        return candidate
    n = 2
    while f"{candidate}-{n}" in taken:
        n += 1
    return f"{candidate}-{n}"


def _coerce_to_site(raw: dict, *, taken_ids: set[str]) -> Site:
    """Build a Site from a raw YAML dict, synthesizing missing fields.

    Normalizes the legacy `namd:` typo (matches the migration script's
    behavior) so loader and migration produce identical ids for the same
    file. After normalization, the **full** dict is passed through
    `Site.model_validate` so Pydantic's `extra='forbid'` catches typos like
    `idd:` or `urll:` — which would otherwise silently drop and the loader
    would auto-generate a (probably wrong) id from `name`.

    Mutates `taken_ids` to register the resolved id.
    """
    if not isinstance(raw, dict):
        raise TypeError(f"Site entry must be a dict, got {type(raw).__name__}: {raw!r}")

    # Normalize the legacy `namd:` typo BEFORE validation. The migration
    # script does this same fix-up; doing it here too means a not-yet-
    # migrated file still loads cleanly and auto-generates the same id
    # that `migrate_sites_ids.py` would write.
    fixed = dict(raw)
    if "namd" in fixed and "name" not in fixed:
        fixed["name"] = fixed.pop("namd")

    url = fixed.get("url")
    if not url:
        raise ValueError(f"Site entry missing required `url`: {raw!r}")
    name = fixed.get("name") or ""
    explicit_id = fixed.get("id")

    if explicit_id:
        if explicit_id in taken_ids:
            raise ValueError(f"Duplicate id {explicit_id!r} in sites.yml")
        site_id = explicit_id
    else:
        base = slugify(name) if name else slugify(url_to_dirname(url))
        site_id = dedupe_slug(base, taken_ids)
        logger.warning(
            f"sites.yml: auto-generated id {site_id!r} for {url!r}. "
            f"Run `python scripts/migrate_sites_ids.py` to commit it to disk."
        )

    taken_ids.add(site_id)

    # Build the validated dict. We overwrite id+name with the resolved
    # values (vs. raw YAML) and pass through anything else so Pydantic's
    # extra='forbid' rejects unknown keys (e.g. a `idd:` typo).
    final_name = name or url
    payload = {**fixed, "id": site_id, "name": final_name, "url": url}
    return Site.model_validate(payload)


def load_sites(path: str | Path) -> list[Site]:
    """Read `sites.yml` and return validated `Site` objects.

    Auto-fills missing `id` fields (with WARNING) so legacy files still work.
    Raises `ValueError` on duplicate explicit ids or missing `url`.
    """
    raw_text = Path(path).read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw_text) or {}
    # `sites: ` with no value parses to None (not absent + default), so the
    # `or []` fallback covers both "no key" and "key with empty value" cases.
    raw_sites = parsed.get("sites") or []
    if not isinstance(raw_sites, list):
        raise ValueError(
            f"sites.yml `sites:` must be a list, got {type(raw_sites).__name__}"
        )

    taken: set[str] = set()
    return [_coerce_to_site(raw, taken_ids=taken) for raw in raw_sites]


def site_dir_name(site: dict | Site) -> str:
    """Resolve the per-site directory name for `<run_root>/<NAME>/`.

    Phase B.3: prefer `site["id"]` / `site.id` (stable identifier the user
    controls). Fall back to `url_to_dirname(url)` for legacy callers /
    tests that pass `{url, name}` dicts without an id.

    Centralized here so the crawler + comparator can't drift on the
    naming convention — both depend on this returning identical values
    for the same site (otherwise baseline/current/comparator runs can't
    find each other's outputs).
    """
    if isinstance(site, Site):
        return site.id
    if isinstance(site, dict):
        site_id = site.get("id")
        if site_id:
            return site_id
        return url_to_dirname(site["url"])
    raise TypeError(f"site_dir_name() requires Site or dict, got {type(site).__name__}")


__all__ = ["Site", "slugify", "dedupe_slug", "load_sites", "site_dir_name"]
