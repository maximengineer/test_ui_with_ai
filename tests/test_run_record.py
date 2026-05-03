"""Per-run invocation record tests (Phase B.3.4)."""

from __future__ import annotations

import pytest

from test_ui.common.run_record import (
    RunRecord,
    read_run_record,
    write_run_record,
)
from test_ui.config import settings


@pytest.fixture
def isolated_data_root(tmp_path, monkeypatch):
    """Redirect settings.runs_log_dir so the runs/ dir lands in a tmp tree.

    runs_log_dir is the override-able setting `write_run_record` actually
    consumes (defaults to `<data_root>/runs/`); tests here pin only the
    explicit setting to keep them independent of data_root layout.
    """
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(settings, "runs_log_dir", runs_dir)
    return runs_dir


def test_write_creates_runs_dir_and_file(isolated_data_root):
    write_run_record(
        "01HXX0000000000000000000A0",
        kind="baseline",
        args={"output_dir": "data/baseline", "is_baseline": True},
    )
    record_path = isolated_data_root / "01HXX0000000000000000000A0.run.json"
    assert record_path.exists()


def test_round_trip(isolated_data_root):
    write_run_record(
        "01HXX0000000000000000000A0",
        kind="comparator",
        args={"baseline_dir": "/x", "current_dir": "/y", "site_count": 3},
    )

    rec = read_run_record("01HXX0000000000000000000A0")
    assert rec is not None
    assert rec.run_id == "01HXX0000000000000000000A0"
    assert rec.kind == "comparator"
    assert rec.args == {"baseline_dir": "/x", "current_dir": "/y", "site_count": 3}
    assert rec.command  # populated from sys.argv if not overridden


def test_read_returns_none_for_missing(isolated_data_root):
    assert read_run_record("01HZZ0000000000000000000B0") is None


def test_read_returns_none_for_corrupt(isolated_data_root):
    """A corrupt run record should not crash callers — return None and log."""
    isolated_data_root.mkdir()
    (isolated_data_root / "01HXX0000000000000000000A0.run.json").write_text(
        "not json", encoding="utf-8"
    )
    assert read_run_record("01HXX0000000000000000000A0") is None


def test_write_failure_does_not_raise(isolated_data_root, monkeypatch):
    """Best-effort: a write OSError must NOT propagate (the actual run is
    more important than the metadata file)."""
    from pathlib import Path

    real_write_text = Path.write_text

    def _failing_write_text(self, *a, **kw):
        if self.suffix == ".json":
            raise PermissionError("simulated read-only fs")
        return real_write_text(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", _failing_write_text)

    # Must not raise — the underlying OSError is logged but swallowed.
    write_run_record("01HXX0000000000000000000A0", kind="baseline")


def test_command_defaults_to_sys_argv(isolated_data_root, monkeypatch):
    monkeypatch.setattr(
        "sys.argv", ["fake-binary", "snapshot", "--output", "data/baseline"]
    )
    write_run_record("01HXX0000000000000000000A0", kind="baseline")
    rec = read_run_record("01HXX0000000000000000000A0")
    assert rec.command == ["fake-binary", "snapshot", "--output", "data/baseline"]


def test_command_can_be_overridden(isolated_data_root):
    write_run_record(
        "01HXX0000000000000000000A0",
        kind="baseline",
        command=["explicit", "command", "args"],
    )
    rec = read_run_record("01HXX0000000000000000000A0")
    assert rec.command == ["explicit", "command", "args"]


def test_runrecord_extra_forbid():
    """extra='forbid' so a typo'd field name is loud."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RunRecord.model_validate(
            {
                "run_id": "x",
                "kind": "baseline",
                "started_at": "01-01-2099 00:00:00",
                "command": [],
                "rogue": "field",
            }
        )
