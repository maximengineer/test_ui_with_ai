"""Run-lifecycle vocabulary + transition rules for the dashboard.

Why this module exists:
  - `test_ui` manifests use: running|complete|failed|interrupted
  - dashboard DB/API use: pending|running|done|failed|interrupted

The overlap is intentional but not identical. This file is the single place
that defines:
  1) manifest -> dashboard status mapping
  2) active/terminal subsets
  3) allowed status transitions for DB row updates
"""

from __future__ import annotations

from typing import Literal, cast, get_args

from test_ui.common.manifest import Status as ManifestStatus


# Dashboard row status vocabulary.
RunStatus = Literal["pending", "running", "done", "failed", "interrupted"]
RUN_STATUSES_TUPLE: tuple[RunStatus, ...] = cast(
    tuple[RunStatus, ...], get_args(RunStatus)
)

# Shared subsets used by DB helpers and route semantics.
ACTIVE_STATUSES_TUPLE: tuple[RunStatus, ...] = ("pending", "running")
TERMINAL_STATUSES_TUPLE: tuple[RunStatus, ...] = ("done", "failed", "interrupted")
NON_DELETABLE_STATUSES_TUPLE: tuple[RunStatus, ...] = ACTIVE_STATUSES_TUPLE

# Manifest -> dashboard mapping. `complete` is the only renamed value.
_MANIFEST_TO_RUN_STATUS: dict[str, RunStatus] = {
    "running": "running",
    "complete": "done",
    "failed": "failed",
    "interrupted": "interrupted",
}

# Allowed transition matrix for dashboard rows.
#
# Notes:
# - pending -> done is intentionally allowed. In very fast subprocess exits,
#   the watcher can observe process completion before mark_running lands.
# - terminal statuses are absorbing; no transitions out.
_ALLOWED_TARGETS_BY_SOURCE: dict[RunStatus, set[RunStatus]] = {
    "pending": {"running", "done", "failed", "interrupted"},
    "running": {"done", "failed", "interrupted"},
    "done": set(),
    "failed": set(),
    "interrupted": set(),
}


def manifest_status_to_run_status(manifest_status: ManifestStatus | str) -> RunStatus:
    """Translate a manifest status into dashboard row vocabulary.

    Raises ValueError for unknown values so new manifest statuses fail loudly
    until this mapping is updated.
    """
    try:
        return _MANIFEST_TO_RUN_STATUS[str(manifest_status)]
    except KeyError as e:
        allowed = ", ".join(sorted(_MANIFEST_TO_RUN_STATUS))
        raise ValueError(
            f"unknown manifest status {manifest_status!r}; expected one of: {allowed}"
        ) from e


def can_transition(from_status: RunStatus, to_status: RunStatus) -> bool:
    """True iff a row is allowed to move from `from_status` to `to_status`."""
    return to_status in _ALLOWED_TARGETS_BY_SOURCE[from_status]


def transition_sources_for(to_status: RunStatus) -> tuple[RunStatus, ...]:
    """Return statuses that are allowed to transition into `to_status`.

    Deterministic order follows RUN_STATUSES_TUPLE for stable SQL parameter
    ordering and deterministic tests.
    """
    sources = [
        status
        for status in RUN_STATUSES_TUPLE
        if can_transition(status, to_status)
    ]
    return tuple(sources)


def _verify_subset(name: str, values: tuple[RunStatus, ...]) -> None:
    unknown = set(values) - set(RUN_STATUSES_TUPLE)
    if unknown:
        raise RuntimeError(
            f"{name} includes unknown status(es): {sorted(unknown)}; "
            f"allowed={sorted(set(RUN_STATUSES_TUPLE))}"
        )


def _verify_transition_matrix() -> None:
    if set(_ALLOWED_TARGETS_BY_SOURCE) != set(RUN_STATUSES_TUPLE):
        raise RuntimeError(
            "transition matrix keys must exactly match RUN_STATUSES_TUPLE"
        )
    for source, targets in _ALLOWED_TARGETS_BY_SOURCE.items():
        _verify_subset(f"targets for source={source}", tuple(targets))
    for terminal in TERMINAL_STATUSES_TUPLE:
        if _ALLOWED_TARGETS_BY_SOURCE[terminal]:
            raise RuntimeError(
                f"terminal status {terminal!r} must be absorbing "
                f"(got targets={sorted(_ALLOWED_TARGETS_BY_SOURCE[terminal])})"
            )


_verify_subset("ACTIVE_STATUSES_TUPLE", ACTIVE_STATUSES_TUPLE)
_verify_subset("TERMINAL_STATUSES_TUPLE", TERMINAL_STATUSES_TUPLE)
_verify_subset("NON_DELETABLE_STATUSES_TUPLE", NON_DELETABLE_STATUSES_TUPLE)
_verify_transition_matrix()


__all__ = [
    "RunStatus",
    "RUN_STATUSES_TUPLE",
    "ACTIVE_STATUSES_TUPLE",
    "TERMINAL_STATUSES_TUPLE",
    "NON_DELETABLE_STATUSES_TUPLE",
    "manifest_status_to_run_status",
    "can_transition",
    "transition_sources_for",
]
