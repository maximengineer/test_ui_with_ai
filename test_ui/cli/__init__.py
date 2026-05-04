"""CLI entry point + Orchestrator (post-B.3 split from a single 692-LOC file).

Public surface preserved for backward compatibility:
- `cli` - the Click group, used by `test_ui/__main__.py` and tests
- `Orchestrator` - used by `tests/test_lock_workflow.py`
- `_open_orchestrator` - used by `tests/test_e2e_smoke.py`,
  `tests/test_golden_ai_analysis.py`

After the split, the actual code lives in:
- `commands.py` - `cli` group + `_open_orchestrator` + snapshot/current/compare/enhanced-report commands
- `retry.py` - `retry-url` command (split out at 444 LOC review)
- `orchestrator.py` - the `Orchestrator` class

Importing `retry` here is required so its `@cli.command(name="retry-url")`
decorator runs at package-load time and the command attaches to the group.
"""

# Side-effect import: the `@cli.command(name="retry-url")` decorator in
# retry.py runs at module-load time and attaches the command to the group.
# Imported BEFORE the public-name re-exports so any direct user of `cli`
# already sees retry-url registered. Not added to __all__ - its function
# is registration, not re-export. Tests that need the function reference
# can `from test_ui.cli.retry import retry_url` directly.
from . import retry  # noqa: F401

from .commands import _open_orchestrator, cli, console
from .orchestrator import Orchestrator


__all__ = ["cli", "console", "Orchestrator", "_open_orchestrator"]
