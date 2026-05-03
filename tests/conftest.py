"""Shared pytest fixtures + config for the test suite.

Phase A.2 adds the `--update-golden` flag and the `golden_compare` /
`golden_write` helpers that characterization tests use to compare current
output against stored snapshots in `tests/fixtures/golden/`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# --update-golden flag (Phase A.2)
# ---------------------------------------------------------------------------


def pytest_addoption(parser):
    """Register the `--update-golden` flag.

    When set, golden-file comparison helpers REWRITE the golden file with the
    current output instead of asserting equality. Use after intentional
    behavior changes that need new snapshots.

    Usage:
        pytest --update-golden tests/test_e2e_smoke.py::test_html_report_golden
    """
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Regenerate golden snapshot files instead of comparing.",
    )


@pytest.fixture
def update_golden(request) -> bool:
    """True when the test suite was invoked with --update-golden."""
    return bool(request.config.getoption("--update-golden"))


# ---------------------------------------------------------------------------
# Fixture-directory accessors
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_dir() -> Path:
    return FIXTURES_DIR / "golden"


@pytest.fixture
def contracts_dir() -> Path:
    return FIXTURES_DIR / "contracts"


@pytest.fixture
def example_diffs_dir() -> Path:
    return FIXTURES_DIR / "example_diffs"


# ---------------------------------------------------------------------------
# Golden-file comparison helpers
# ---------------------------------------------------------------------------


def _normalize_for_compare(obj: Any, normalize_keys: tuple[str, ...]) -> Any:
    """Recursively replace volatile fields with sentinels.

    Anything matching `normalize_keys` is replaced with `"<NORMALIZED>"`. Used
    for fields like `request_id`, `checked_at`, `timestamp`, `prompt_sha256`
    that vary per-run and would otherwise break byte-equality.
    """
    if isinstance(obj, dict):
        return {
            k: (
                "<NORMALIZED>"
                if k in normalize_keys
                else _normalize_for_compare(v, normalize_keys)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_normalize_for_compare(v, normalize_keys) for v in obj]
    return obj


@pytest.fixture
def golden_compare(golden_dir, update_golden):
    """Return a callable that asserts JSON output matches a golden file.

    Usage:
        def test_X(golden_compare):
            actual = build_something()
            golden_compare(actual, "snapshot_name.json", normalize=("request_id", "checked_at"))

    With `--update-golden`, rewrites the golden file instead of asserting.
    Volatile keys listed in `normalize` are replaced with a sentinel before
    comparison so per-run variance doesn't fail the test.
    """

    def _cmp(
        actual: dict | list,
        golden_filename: str,
        *,
        normalize: tuple[str, ...] = (),
        subdir: str = "",
    ) -> None:
        target = (
            (golden_dir / subdir / golden_filename)
            if subdir
            else (golden_dir / golden_filename)
        )
        normalized_actual = _normalize_for_compare(actual, normalize)
        actual_json = json.dumps(normalized_actual, indent=2, sort_keys=True) + "\n"

        if update_golden or not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(actual_json, encoding="utf-8")
            if not update_golden:
                pytest.fail(
                    f"Golden file {target} did not exist; created it. "
                    "Re-run the test to confirm it passes."
                )
            return  # --update-golden: silently rewrote

        expected_json = target.read_text(encoding="utf-8")
        if expected_json != actual_json:
            # Print a unified diff to make the failure scannable.
            import difflib

            diff = "".join(
                difflib.unified_diff(
                    expected_json.splitlines(keepends=True),
                    actual_json.splitlines(keepends=True),
                    fromfile=f"golden:{target.name}",
                    tofile="actual",
                )
            )
            pytest.fail(
                f"Golden mismatch for {target.relative_to(golden_dir.parent.parent)}.\n"
                f"Run `pytest --update-golden` to accept current output.\n"
                f"\n{diff}"
            )

    return _cmp
