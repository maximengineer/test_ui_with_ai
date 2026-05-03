"""Add stable `id:` fields to sites.yml entries that lack one (Phase B.3.2).

For each site without an explicit `id`, derives one from the slugified
`name` (or `url` if name is missing/typo'd) and writes the file back.
Idempotent — running twice is a no-op.

Uses `ruamel.yaml` (round-trip mode) instead of `pyyaml` so comments,
blank lines, key order, and quoting style survive the rewrite. The
operator's editorial choices in sites.yml are preserved; only `id:`
keys get added (or — for the legacy `namd:` typo — replaced with `name:`).

Usage::

    python scripts/migrate_sites_ids.py [path/to/sites.yml]

Default path: `test_ui/sites.yml` relative to the project root.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ruamel.yaml import YAML  # noqa: E402

from test_ui.common.sites import dedupe_slug, slugify  # noqa: E402
from test_ui.common.url_id import url_to_dirname  # noqa: E402


DEFAULT_SITES_PATH = REPO_ROOT / "test_ui" / "sites.yml"


def migrate(sites_path: Path) -> tuple[int, int]:
    """Mutate sites.yml in place to add ids. Returns (added_count, total_count).

    Strategy:
      1. Round-trip parse (preserves comments / formatting).
      2. For each site, fix the legacy `namd:` typo → `name:`.
      3. If no `id:` present, synthesize one from the (now-fixed) name,
         falling back to url_to_dirname(url) if name is empty. Dedupe
         against ids already chosen in this file.
      4. Write back with the same yaml_encoder.
    """
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)

    with sites_path.open("r", encoding="utf-8") as f:
        data = yaml.load(f)

    if data is None or "sites" not in data:
        print(f"No `sites:` key found in {sites_path}; nothing to do.")
        return (0, 0)

    sites = data["sites"]
    if not isinstance(sites, list):
        raise SystemExit(
            f"sites.yml `sites:` must be a list, got {type(sites).__name__}"
        )

    # First pass: collect explicit ids so the synthesized ones don't collide.
    taken: set[str] = set()
    for site in sites:
        if isinstance(site, dict) and "id" in site:
            taken.add(site["id"])

    added = 0
    for site in sites:
        if not isinstance(site, dict):
            continue

        # Fix the historical typo `namd:` → `name:`. ruamel preserves order,
        # so we copy the value into a real `name:` key and remove the typo.
        if "namd" in site and "name" not in site:
            site["name"] = site.pop("namd")

        if "id" in site:
            continue

        url = site.get("url")
        name = site.get("name") or ""
        if not url:
            print(f"  ⚠ skipping entry with no url: {dict(site)}", file=sys.stderr)
            continue

        base = slugify(name) if name else slugify(url_to_dirname(url))
        new_id = dedupe_slug(base, taken)
        taken.add(new_id)

        # Insert `id:` as the FIRST key for human readability.
        site.insert(0, "id", new_id)
        added += 1
        print(f"  + {new_id}  ←  {url}")

    if added == 0:
        print(f"All {len(sites)} sites already have ids. No changes written.")
        return (0, len(sites))

    with sites_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f)
    print(f"\n✓ Added {added} ids (of {len(sites)} total) to {sites_path}")
    return (added, len(sites))


def main() -> int:
    args = sys.argv[1:]
    if len(args) > 1:
        print(
            "Usage: python scripts/migrate_sites_ids.py [path/to/sites.yml]",
            file=sys.stderr,
        )
        return 2
    path = Path(args[0]) if args else DEFAULT_SITES_PATH

    if not path.exists():
        print(f"sites.yml not found at {path}", file=sys.stderr)
        return 1

    migrate(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
