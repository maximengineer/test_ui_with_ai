"""Audit source files for hardcoded `data/` paths.

Phase A.0.3 deliverable. Run via `make audit` or in CI.

Exits 0 if no hits, 1 if any are found. Patterns covered:
    "data/    'data/    Path("data    Path('data
    os.path.join("data    os.path.join('data
    f"data/    f'data/

The grep is regex-based on text, not AST. False positives are possible inside
strings/comments that happen to look like paths. False negatives are possible
for variables holding "data/..." strings or for unusual quoting. Treat the
output as a starting point for review.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Source directories to audit. Skipped silently if missing (e.g. dashboard/
# doesn't exist until Milestone C).
SOURCE_DIRS = ["test_ui", "scripts", "dashboard"]

# Files we never want to flag - generated, vendored, or by-design references.
EXCLUDE = {
    REPO_ROOT / "scripts" / "audit_paths.py",  # this file
    REPO_ROOT / "test_ui" / "config.py",  # canonical defaults live here
}

PATTERNS = [
    re.compile(r'"data/'),
    re.compile(r"'data/"),
    re.compile(r'Path\("data'),
    re.compile(r"Path\('data"),
    re.compile(r'os\.path\.join\("data'),
    re.compile(r"os\.path\.join\('data"),
    re.compile(r'f"data/'),
    re.compile(r"f'data/"),
]


def audit() -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []
    for source_name in SOURCE_DIRS:
        source = REPO_ROOT / source_name
        if not source.exists():
            continue
        for py_file in source.rglob("*.py"):
            if py_file in EXCLUDE:
                continue
            try:
                text = py_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if any(p.search(line) for p in PATTERNS):
                    hits.append((py_file, line_no, line.strip()))
    return hits


def main() -> int:
    hits = audit()
    if not hits:
        print("audit_paths: clean (no hardcoded data/ paths in source).")
        return 0

    print(f"audit_paths: {len(hits)} hit(s) - replace with settings.<path>:")
    for path, line_no, snippet in hits:
        rel = path.relative_to(REPO_ROOT)
        print(f"  {rel}:{line_no}: {snippet}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
