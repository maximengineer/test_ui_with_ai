"""Schema drift test (Phase A.4).

Verifies that the JSON Schema files in `schemas/` are byte-identical to what
`scripts/export_schemas.py` would currently produce from the Pydantic source
of truth in `test_ui/contracts/ai_contract.py`.

If this test fails, you've changed a Pydantic model without re-running the
exporter. Run:

    python scripts/export_schemas.py && git diff schemas/

…then commit the regenerated schemas.

Why this matters: ajv (Node side) loads `schemas/*.schema.json` directly. If
Pydantic and the emitted schema disagree, the Python and Node validators
will accept/reject different inputs, and the ai_analyzer service will
silently mismatch what the contract test_smoke.py + Pydantic say is valid.

Implementation: regenerates schemas in-memory and compares to on-disk files
WITHOUT writing anything. Doesn't need git, doesn't shell out, runs in
milliseconds. Equivalent to `python scripts/export_schemas.py && git diff
--exit-code schemas/` but contained inside pytest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Reuse the exporter's own EXPORTS map so the test can never go stale: if
# someone adds a model to EXPORTS, the test picks it up automatically.
from scripts.export_schemas import EXPORTS, SCHEMAS_DIR


@pytest.mark.parametrize(
    "filename,model_cls",
    [(name, cls) for name, cls in EXPORTS.items()],
    ids=list(EXPORTS.keys()),
)
def test_schema_on_disk_matches_pydantic_export(filename, model_cls):
    """Byte-equality between on-disk schema and freshly-exported schema.

    `export_schemas.py` writes `json.dumps(schema, indent=2, sort_keys=True) + "\\n"`.
    Mirror that exactly so byte-equality is meaningful.
    """
    schema_path: Path = SCHEMAS_DIR / filename
    assert schema_path.exists(), (
        f"schema file missing: {schema_path}. "
        f"Run `python scripts/export_schemas.py` to regenerate."
    )

    on_disk = schema_path.read_text(encoding="utf-8")
    fresh = json.dumps(model_cls.model_json_schema(), indent=2, sort_keys=True) + "\n"

    assert on_disk == fresh, (
        f"\n{filename} on disk differs from Pydantic export.\n"
        f"Pydantic model `{model_cls.__name__}` has changed without "
        f"re-running scripts/export_schemas.py.\n"
        f"Fix: run `python scripts/export_schemas.py` and commit the "
        f"updated schemas."
    )


def test_every_exported_schema_is_listed_in_validator_dispatch():
    """The Node validator (ai_analyzer/scripts/validate.js) and the
    Python contract smoke test (tests/test_contract_smoke.py) both use a
    schema-name → model map. If a new schema lands in EXPORTS but isn't
    wired into the smoke test's SCHEMA_TO_MODEL, fixtures referencing it
    would fail with 'Unknown schema name'. Catch that early."""
    from tests.test_contract_smoke import SCHEMA_TO_MODEL

    # EXPORTS keys are like 'ai_request.schema.json'; SCHEMA_TO_MODEL keys
    # are the basename without the suffix ('ai_request').
    exported_basenames = {fn.replace(".schema.json", "") for fn in EXPORTS}
    smoke_basenames = set(SCHEMA_TO_MODEL.keys())

    missing = exported_basenames - smoke_basenames
    assert not missing, (
        f"Schemas listed in EXPORTS but not in test_contract_smoke.SCHEMA_TO_MODEL: "
        f"{sorted(missing)}. Add them to SCHEMA_TO_MODEL so the cross-language "
        f"contract test can validate fixtures targeting them."
    )
