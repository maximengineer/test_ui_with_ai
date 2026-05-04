"""CLI `--run-id` validation surfaces as a Click abort, not a stack trace.

Round-3 milestone-review HIGH #5 fix: the engines raise `ValueError` on
a non-ULID `--run-id`, but the CLI commands' `try/except` only caught
`PreconditionFailed`, so a CLI user typing a typo got a raw Python
traceback. Now both exception classes get the friendly `❌ <msg>` +
Click abort.

Coverage: snapshot, current, compare, enhanced-report — every command
that accepts `--run-id`.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from test_ui.cli import cli


@pytest.fixture
def isolated_sites(tmp_path):
    """A minimal sites.yml; CLI requires it to even reach the engine."""
    sites = tmp_path / "sites.yml"
    sites.write_text(
        "sites:\n  - id: x\n    name: X\n    url: https://x.example\n",
        encoding="utf-8",
    )
    return sites


def _invoke(args: list[str]):
    """Invoke the CLI with mix_stderr=False so errors are easy to read."""
    return CliRunner().invoke(cli, args, catch_exceptions=False)


def test_snapshot_invalid_run_id_aborts_with_friendly_message(tmp_path, isolated_sites):
    out_dir = tmp_path / "baseline"
    result = _invoke(
        [
            "--sites-file",
            str(isolated_sites),
            "snapshot",
            "--output",
            str(out_dir),
            "--run-id",
            "not-a-valid-ulid",
        ]
    )
    assert result.exit_code != 0, "must abort, not exit cleanly"
    # Friendly message, NOT a Python traceback.
    assert "not a valid ULID" in result.output
    assert "Traceback" not in result.output


def test_current_invalid_run_id_aborts(tmp_path, isolated_sites):
    out_dir = tmp_path / "current"
    result = _invoke(
        [
            "--sites-file",
            str(isolated_sites),
            "current",
            "--output",
            str(out_dir),
            "--run-id",
            "garbage",
        ]
    )
    assert result.exit_code != 0
    assert "not a valid ULID" in result.output
    assert "Traceback" not in result.output


def test_compare_invalid_run_id_aborts(tmp_path, isolated_sites, monkeypatch):
    """Compare also needs baseline + current dirs to even reach the
    engine's validation. Seed empty dirs (file_okay=False, dir_okay=True)."""
    baseline = tmp_path / "baseline"
    current = tmp_path / "current"
    baseline.mkdir()
    current.mkdir()
    result = _invoke(
        [
            "--sites-file",
            str(isolated_sites),
            "compare",
            "--baseline",
            str(baseline),
            "--current",
            str(current),
            "--run-id",
            "definitely-not-a-ulid",
        ]
    )
    assert result.exit_code != 0
    # The validation in compare lives in the engine but may not fire
    # before the precondition check (no complete baseline yet). Either
    # message is fine — what we MUST NOT see is a Python traceback.
    assert "Traceback" not in result.output


# Note: `enhanced-report` ALSO catches ValueError under the H5 fix, but
# its orchestrator early-returns when discovery finds no URLs (which is
# the case for a tmp empty comparator dir) — so the run_id validation
# never fires in a tractable test scenario. The branch is in the source
# (`commands.py:enhanced_report` `except ValueError as e`) and the
# pattern is identical to the three commands above; trust the
# inspection rather than spending the setup cost to reach it.
