"""Export Pydantic contract models to JSON Schema files in schemas/.

Phase A.1.2 deliverable. Run via `python scripts/export_schemas.py`.

CI uses this script + `git diff --exit-code schemas/` to detect drift between
the Pydantic source of truth (test_ui/contracts/ai_contract.py) and the
emitted schemas that Node consumes via ajv.

Output is sorted-keys JSON so git diffs are stable across Python versions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from test_ui.contracts.ai_contract import (  # noqa: E402
    AIAnalysisError,
    AIAnalysisRequest,
    AIAnalysisResponse,
    AIDisabledMarker,
    NoChangesMarker,
)

SCHEMAS_DIR = REPO_ROOT / "schemas"

EXPORTS = {
    "ai_request.schema.json": AIAnalysisRequest,
    "ai_response.schema.json": AIAnalysisResponse,
    "ai_error.schema.json": AIAnalysisError,
    "no_changes.schema.json": NoChangesMarker,
    "ai_disabled.schema.json": AIDisabledMarker,
}


def export() -> None:
    SCHEMAS_DIR.mkdir(exist_ok=True)
    for filename, model in EXPORTS.items():
        schema = model.model_json_schema()
        target = SCHEMAS_DIR / filename
        target.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
        print(f"wrote {target.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    export()
