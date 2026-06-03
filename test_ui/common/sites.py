"""Site list loader + `Site` model (Phase B.3.1).

A site is `{id, name, url}` where `id` is the **stable identifier** the rest
of the pipeline uses for per-site directory names (e.g.
`data/baseline/<date>/<run_id>/<id>/`). Naming the data dirs by `id` instead
of by URL means a URL change (e.g. site moves from `/about` to `/about-us`)
keeps history continuous - same id, same data path, comparator can still
diff baseline vs. current.

`name` is human-facing display text. It can change freely without touching
on-disk identifiers.

**Loader behaviour for backward compatibility (pre-B.3 sites.yml):**

  - If `id` is missing in YAML, we synthesize one from `name` (slugified +
    deduplicated against other sites in the same file) and emit a WARNING
    pointing the operator at `scripts/migrate_sites_ids.py` to write the
    ids back to disk.
  - If `name` is also missing or empty, we fall back to slugifying the URL
    via `url_to_dirname` so the loader never raises on legacy data - but
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
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import settings
from .url_id import url_to_dirname
from .url_safety import UnsafeCrawlURL, validate_crawl_url


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Lowercase + replace non-alphanumeric with `-` + collapse + strip.

    Stable, deterministic, ASCII-only. Doesn't try to romanize Unicode -
    operators with non-ASCII names should set `id:` explicitly.
    """
    out = _SLUG_RE.sub("-", value.lower()).strip("-")
    return out or "site"  # never return empty


class Site(BaseModel):
    """A single entry in sites.yml. `id` is the stable filesystem-safe key.

    `name` and `url` are min_length=1 so a typo that produces an empty
    string fails at model construction, not at the next pipeline stage
    that tries to read a directory named ''. The loader already raises
    on missing `url`; this catches programmatic callers (CRUD routes,
    migrations) that bypass the loader.

    `max_length` caps are generous-but-bounded: 200 for name (display
    text - anything longer is a copy-paste accident), 2048 for url
    (the practical HTTP URL ceiling - RFC 7230 doesn't impose one but
    most stacks do). Stops a 10MB-name DoS from bloating sites.yml
    and slowing down every subsequent load.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2048)

    @field_validator("url")
    @classmethod
    def _validate_crawl_target(cls, value: str) -> str:
        try:
            return validate_crawl_url(
                value, allow_private=settings.allow_private_site_urls
            )
        except UnsafeCrawlURL as e:
            raise ValueError(str(e)) from e


def dedupe_slug(candidate: str, taken: set[str]) -> str:
    """Append `-2`, `-3`, ... until the slug is unique relative to `taken`.

    Used ONLY by the legacy-yaml loader path (`_coerce_to_site` for
    entries that lack an explicit `id`). New `add_site` calls use
    `next_numeric_id` instead - operator-facing IDs are numeric per the
    user's preference. Kept here for backward compatibility with any
    pre-numeric sites.yml that an operator hasn't yet migrated.
    """
    if candidate not in taken:
        return candidate
    n = 2
    while f"{candidate}-{n}" in taken:
        n += 1
    return f"{candidate}-{n}"


def next_numeric_id(taken: set[str]) -> str:
    """Return the smallest unused positive integer (as a string).

    Iterates 1, 2, 3, ... and returns the first not in `taken`. Stable
    even when the existing set is sparse (e.g. {"1", "3"} -> "2"), so
    deletes don't leave permanent gaps in the sequence.

    Why string-typed: the on-disk YAML stores IDs as strings (the model
    field is `str`), and our pattern `^[a-z0-9][a-z0-9_-]*$` accepts
    purely-numeric strings, so this is a drop-in replacement for
    `dedupe_slug` in the auto-generation path. Returning an int would
    just force every caller to `str(...)` it.
    """
    n = 1
    while str(n) in taken:
        n += 1
    return str(n)


def _coerce_to_site(raw: dict, *, taken_ids: set[str]) -> Site:
    """Build a Site from a raw YAML dict, synthesizing missing fields.

    Normalizes the legacy `namd:` typo (matches the migration script's
    behavior) so loader and migration produce identical ids for the same
    file. After normalization, the **full** dict is passed through
    `Site.model_validate` so Pydantic's `extra='forbid'` catches typos like
    `idd:` or `urll:` - which would otherwise silently drop and the loader
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


# --------------------------------------------------------------------------- #
# CRUD (Phase C.2 - dashboard slice).                                         #
# --------------------------------------------------------------------------- #
#
# These helpers mutate `sites.yml` for the dashboard's POST/PATCH/DELETE
# routes. They use `ruamel.yaml` round-trip mode so operator-authored
# comments survive the round-trip - `yaml.safe_dump` would silently drop
# them, which is the worst kind of UX regression for an ops-edited file.
#
# Writes are atomic (`tmp + rename`) so a crash mid-write can't leave a
# half-formed YAML on disk that breaks the next dashboard startup.


# Note: there is intentionally NO `SiteAlreadyExists` exception. The Site
# CRUD doesn't expose `id` as a client-settable field - `add_site` always
# auto-generates the id by slugifying `name` and appending a numeric
# suffix on collision (`-2`, `-3`, …). So an "id already exists" condition
# is structurally impossible from the public API. Two sites with the same
# `name` get distinct ids by design (operator may genuinely have two
# subsections of the same site).


class SiteNotFound(ValueError):
    """No site with the given id exists in the file."""


def _ruamel_yaml():
    """Return a ruamel.yaml YAML() configured for round-trip preservation.

    Constructed lazily so the import cost is paid only by callers that
    actually mutate the file; the read path (`load_sites`) stays on PyYAML
    which is faster and good enough for read-only validation.
    """
    from ruamel.yaml import YAML

    y = YAML(typ="rt")
    # Preserve quotes / structure as much as possible. The defaults are
    # already round-trip-friendly; explicit settings here are for clarity
    # and to pin the behavior against a future ruamel default change.
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _atomic_write_yaml(path: Path, data) -> None:
    """Write `data` to `path` via tmp+rename (atomic on the same FS).

    Uses ruamel's dump so round-trip-preserved structures (comments,
    quote styles) survive. The tmp file goes in the same directory so the
    rename stays atomic - across-filesystem renames are NOT atomic and
    would defeat the point.
    """
    yaml_rt = _ruamel_yaml()
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            yaml_rt.dump(data, f)
        tmp.replace(path)
    except Exception:
        # Clean up the half-written tmp so a retry doesn't see stale state.
        tmp.unlink(missing_ok=True)
        raise


def _load_for_mutation(path: Path):
    """Load `sites.yml` with ruamel for in-place mutation.

    Returns the parsed structure. Mutates are applied to it directly,
    then `_atomic_write_yaml` writes it back with comments preserved.
    """
    yaml_rt = _ruamel_yaml()
    with path.open(encoding="utf-8") as f:
        data = yaml_rt.load(f) or {}
    if "sites" not in data or data["sites"] is None:
        # File exists but `sites:` is empty/missing - initialize so callers
        # can append without a None-check. ruamel's CommentedSeq behaves
        # like a list for our purposes.
        data["sites"] = []
    return data


def _verify_full_load(path: Path) -> None:
    """Ensure the post-write file still loads cleanly via `load_sites`.

    Defends against a write that succeeded structurally but produced a
    file that the read path can't validate - e.g. a pre-existing entry
    with an invalid id pattern that wasn't a problem until our write
    forced a re-validate. We deliberately call this with the strict
    Pydantic loader so any latent corruption is surfaced immediately
    rather than at the next dashboard read.
    """
    load_sites(path)


def _atomic_rollback(path: Path, original_bytes: bytes | None) -> None:
    """Restore `path` to `original_bytes` (or remove it if it didn't
    exist before the failed write). Atomic via tmp+rename - a crash
    mid-rollback can't leave the file half-overwritten.

    Round-3 review caught that the previous rollback used a plain
    `path.write_bytes()`, which is non-atomic: a crash between truncation
    and write completion would leave the file empty. The tmp+rename
    pattern below matches `_atomic_write_yaml`'s atomicity guarantee.
    """
    if original_bytes is None:
        # File didn't exist pre-write - undo by removing.
        path.unlink(missing_ok=True)
        return
    tmp = path.with_suffix(path.suffix + ".rollback")
    try:
        tmp.write_bytes(original_bytes)
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _verify_or_rollback(path: Path, original_bytes: bytes | None) -> None:
    """Run `_verify_full_load(path)`. On failure, atomically restore
    `original_bytes` and re-raise. Shared between `add_site` and
    `delete_site` (round-3 #M5 made this symmetric - pre-fix only
    `add_site` rolled back, leaving `delete` to wedge the file in
    a permanently-unloadable state on pre-existing corruption).
    """
    try:
        _verify_full_load(path)
    except Exception:
        _atomic_rollback(path, original_bytes)
        raise


def add_site(path: Path, *, name: str, url: str) -> Site:
    """Append a new site. The id is auto-assigned as the smallest unused
    positive integer (1, 2, 3, ...) via `next_numeric_id`. There is no
    "id already exists" failure mode by design (see the SiteNotFound
    block comment above for the rationale).

    Validates name + url via the `Site` Pydantic model BEFORE touching
    the file, so an empty/invalid input fails fast. After the atomic
    write succeeds, ALSO does a full `load_sites(path)` round-trip - if
    a pre-existing entry was already corrupt, the write that reformatted
    the file will surface it now. Rolls back the write on validation
    failure.
    """
    data = _load_for_mutation(path)
    existing_ids: set[str] = {
        s.get("id") for s in data["sites"] if isinstance(s, dict) and s.get("id")
    }

    new_id = next_numeric_id(existing_ids)

    # Validate the new entry via Pydantic BEFORE the file is touched.
    site = Site(id=new_id, name=name, url=url)

    # Backup the original bytes so we can roll back if post-write
    # validation finds the file is now unloadable.
    original_bytes = path.read_bytes() if path.exists() else None

    data["sites"].append({"id": new_id, "name": name, "url": url})
    _atomic_write_yaml(path, data)
    _verify_or_rollback(path, original_bytes)
    return site


def bulk_add_sites(path: Path, urls: list[str]) -> list[Site]:
    """Append a batch of sites at once. id + name auto-generated.

    Each URL becomes one site:
      - `id` is the next sequential integer (1, 2, 3, ...) per the
        `next_numeric_id` strategy, dedup'd across the existing file
        AND across the in-flight batch (so a 5-URL batch added to a
        2-site file produces ids 3, 4, 5, 6, 7).
      - `name` defaults to the URL itself - operator can rename later
        via PATCH if they want a friendlier label.
      - `url` validated via the `Site` Pydantic model; any single URL
        failing validation aborts the WHOLE batch (atomic write means
        nothing lands).

    All-or-nothing semantics by design: half a batch succeeding and
    half failing produces a confusing partial state. Operator gets one
    clear error pointing at the bad URL and re-submits a corrected list.

    Empty `urls` is a no-op (returns []) - lets the route handler
    accept an empty list without special-casing.
    """
    if not urls:
        return []

    data = _load_for_mutation(path)
    existing_ids: set[str] = {
        s.get("id") for s in data["sites"] if isinstance(s, dict) and s.get("id")
    }

    # Validate ALL entries first (and assign ids) so we never write a
    # partially-valid batch. `taken` grows as we walk the batch so each
    # URL gets a distinct id.
    taken = set(existing_ids)
    new_sites: list[Site] = []
    new_entries: list[dict] = []
    for url in urls:
        new_id = next_numeric_id(taken)
        # Validate via Pydantic - throws ValidationError on empty url etc.
        site = Site(id=new_id, name=url, url=url)
        new_sites.append(site)
        new_entries.append({"id": new_id, "name": url, "url": url})
        taken.add(new_id)

    original_bytes = path.read_bytes() if path.exists() else None
    data["sites"].extend(new_entries)
    _atomic_write_yaml(path, data)
    _verify_or_rollback(path, original_bytes)
    return new_sites


def update_site(
    path: Path,
    site_id: str,
    *,
    name: str | None = None,
    url: str | None = None,
) -> Site:
    """Mutate `name` and/or `url` on the site with `site_id`.

    `id` is intentionally immutable - changing it would invalidate every
    existing data dir for that site (per-site dirs are named by id), and
    the dashboard surfaces this as 404 if the operator tries to PATCH a
    different id. To "rename" the id, delete + re-create.

    Raises `SiteNotFound` if no site has `site_id`. Raises `ValidationError`
    if the proposed new values fail the `Site` schema.

    No "no-op fast path" for `name=None, url=None`: the previous version
    short-circuited via `load_sites` (a heavier full re-validation than
    the rewrite path it claimed to optimize). The single-pass approach
    here is faster AND simpler - finds the entry via the in-memory
    parse from `_load_for_mutation`, returns it as-is without rewriting
    when nothing actually changed.
    """
    data = _load_for_mutation(path)
    for entry in data["sites"]:
        if isinstance(entry, dict) and entry.get("id") == site_id:
            new_name = name if name is not None else entry["name"]
            new_url = url if url is not None else entry["url"]
            # Re-validate through the model - catches an empty new url, etc.
            validated = Site(id=site_id, name=new_name, url=new_url)
            # Skip the write if nothing actually changed. Cheaper than
            # rewriting + safer (preserves mtime so file watchers don't
            # spuriously fire).
            if entry.get("name") != new_name or entry.get("url") != new_url:
                entry["name"] = new_name
                entry["url"] = new_url
                _atomic_write_yaml(path, data)
            return validated
    raise SiteNotFound(f"no site with id={site_id!r}")


def delete_site(path: Path, site_id: str) -> None:
    """Remove the site with `site_id`. Raises `SiteNotFound` if absent.

    The on-disk per-site data dirs (`data/baseline/<date>/<run_id>/<id>/`)
    are NOT touched. Removing a site from sites.yml just stops future
    runs from including it; historical artifacts stay so old reports
    remain readable.

    Rolls back the write if the resulting file fails to load (e.g. a
    pre-existing entry has invalid id pattern that round-tripping
    exposed). Round-3 review #M5 caught the asymmetry - `add_site` had
    rollback but `delete_site` didn't.
    """
    data = _load_for_mutation(path)
    sites = data["sites"]
    for i, entry in enumerate(sites):
        if isinstance(entry, dict) and entry.get("id") == site_id:
            original_bytes = path.read_bytes()
            del sites[i]
            _atomic_write_yaml(path, data)
            _verify_or_rollback(path, original_bytes)
            return
    raise SiteNotFound(f"no site with id={site_id!r}")


def bulk_delete_sites(path: Path, ids: list[str]) -> tuple[list[str], list[str]]:
    """Remove a batch of sites in a SINGLE atomic read+write.

    Returns ``(deleted, skipped_not_found)`` - lists of ids that were
    actually removed and ids that weren't present in the file. The
    operator's request is best-effort per-id (mirrors the runs/bulk-delete
    semantics) so a list with stale ids doesn't fail the whole call.

    Why the dedicated helper (vs. looping `delete_site`):
      - Atomic: one read, one write, one rollback check. Looping
        single-deletes would do N read/write/rollback cycles, multiplying
        I/O and widening the window where a partial-failure could leave
        sites.yml in a half-deleted state if the process died mid-loop.
      - Comment preservation: every `_atomic_write_yaml` round-trip is a
        re-emission of the YAML; doing it once preserves operator-authored
        layout exactly once instead of N times.

    Empty `ids` is a no-op (returns ``([], [])``) - lets the route handler
    accept an empty list without special-casing. (The route still rejects
    empty bodies via Pydantic `min_length=1` for the same operator-error
    feedback the runs/bulk-delete endpoint provides.)
    """
    if not ids:
        return [], []

    data = _load_for_mutation(path)
    sites = data["sites"]
    requested = set(ids)
    # Walk in REVERSE so index-based deletion stays valid as we shrink the
    # list. (Forward iteration would skip the entry after each deletion.)
    deleted: list[str] = []
    for i in range(len(sites) - 1, -1, -1):
        entry = sites[i]
        if isinstance(entry, dict) and entry.get("id") in requested:
            deleted.append(entry["id"])
            del sites[i]

    skipped_not_found = sorted(requested - set(deleted))

    if not deleted:
        # Nothing actually changed - skip the write to preserve mtime
        # (file watchers / reloaders are happier this way) and avoid an
        # unnecessary atomic-rename.
        return [], skipped_not_found

    original_bytes = path.read_bytes()
    _atomic_write_yaml(path, data)
    _verify_or_rollback(path, original_bytes)
    # Sort so callers (and tests) see a deterministic order regardless of
    # the order ids appeared in the request or the file.
    return sorted(deleted), skipped_not_found


def site_dir_name(site: dict | Site) -> str:
    """Resolve the per-site directory name for `<run_root>/<NAME>/`.

    Phase B.3: prefer `site["id"]` / `site.id` (stable identifier the user
    controls). Fall back to `url_to_dirname(url)` for legacy callers /
    tests that pass `{url, name}` dicts without an id.

    Centralized here so the crawler + comparator can't drift on the
    naming convention - both depend on this returning identical values
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


__all__ = [
    "Site",
    "SiteNotFound",
    "slugify",
    "dedupe_slug",
    "next_numeric_id",
    "load_sites",
    "site_dir_name",
    "add_site",
    "bulk_add_sites",
    "update_site",
    "delete_site",
    "bulk_delete_sites",
]
