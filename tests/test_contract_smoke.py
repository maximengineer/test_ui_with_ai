"""Cross-language contract smoke test (Phase A.1.6).

Loads each fixture pair in tests/fixtures/contracts/ and validates the data
against its target schema using BOTH validators:

  1. Pydantic (in-process) - via the models in test_ui/contracts/ai_contract.py
  2. ajv (subprocess) - via ai_analyzer/scripts/validate.js

Asserts both validators agree with each other AND with the verdict declared in
the fixture's `.expected.json`. If Pydantic and ajv disagree on the same input,
the test fails loudly - a Pydantic-emitted JSON Schema doesn't match what ajv
strict mode actually enforces, and we want to know now.

A.1.6 ships only the smoke pair (one valid, one invalid). The full matrix is
A.4 - this same test will pick those up automatically as fixture pairs are
added; no test code change needed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from test_ui.contracts.ai_contract import (
    AIAnalysisError,
    AIAnalysisRequest,
    AIAnalysisResponse,
    AIDisabledMarker,
    NoChangesMarker,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
NODE_VALIDATOR = REPO_ROOT / "ai_analyzer" / "scripts" / "validate.js"


# Schema name → Pydantic class. Must mirror the schema basenames in schemas/
# (i.e. <name>.schema.json) and the validate.js CLI's expected schema-name arg.
SCHEMA_TO_MODEL = {
    "ai_request": AIAnalysisRequest,
    "ai_response": AIAnalysisResponse,
    "ai_error": AIAnalysisError,
    "no_changes": NoChangesMarker,
    "ai_disabled": AIDisabledMarker,
}


def _validate_with_pydantic(data: dict, schema_name: str) -> bool:
    """Try to construct the Pydantic model. True if validation passes, else False."""
    model_cls = SCHEMA_TO_MODEL.get(schema_name)
    if model_cls is None:
        raise ValueError(f"Unknown schema name in fixture: {schema_name}")
    try:
        model_cls.model_validate(data)
    except ValidationError:
        return False
    return True


def _validate_with_ajv(fixture_path: Path, schema_name: str) -> tuple[bool, str]:
    """Subprocess-call the Node validator. Returns (valid, stderr)."""
    if shutil.which("node") is None:
        pytest.skip("node executable not on PATH; cannot run cross-language smoke test")
    result = subprocess.run(
        ["node", str(NODE_VALIDATOR), str(fixture_path), schema_name],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"validate.js exited {result.returncode} for {fixture_path.name}: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    if result.stdout == "V":
        return True, result.stderr
    if result.stdout == "I":
        return False, result.stderr
    raise RuntimeError(
        f"validate.js produced unexpected stdout for {fixture_path.name}: "
        f"{result.stdout!r} (expected 'V' or 'I')"
    )


def _discover_fixture_pairs(contracts_dir: Path) -> list[tuple[Path, Path]]:
    """Find every <name>.json + <name>.expected.json pair."""
    pairs = []
    for expected_file in sorted(contracts_dir.glob("*.expected.json")):
        fixture_file = expected_file.with_suffix("").with_suffix(".json")
        if fixture_file.exists() and fixture_file != expected_file:
            pairs.append((fixture_file, expected_file))
    return pairs


def pytest_generate_tests(metafunc):
    """Parameterize tests by every fixture pair we find at collection time."""
    if "fixture_pair" in metafunc.fixturenames:
        contracts_dir = Path(__file__).parent / "fixtures" / "contracts"
        pairs = _discover_fixture_pairs(contracts_dir)
        metafunc.parametrize(
            "fixture_pair",
            pairs,
            ids=[fixture_file.stem for fixture_file, _ in pairs],
        )


def test_validator_cli_exists():
    """The Node validator script must be present where the pytest test expects."""
    assert NODE_VALIDATOR.exists(), (
        f"validator script missing at {NODE_VALIDATOR}. Did Phase A.1.6 ship?"
    )


def test_at_least_one_fixture_pair_exists():
    """Guard against silent test loss.

    pytest_generate_tests parameterizes test_pydantic_and_ajv_agree by however
    many fixture pairs exist. If someone deletes them all, that test would
    silently not run - pytest reports zero failures, looks green, but the
    contract is unverified. This explicit count check fails loudly instead.
    """
    contracts_dir = Path(__file__).parent / "fixtures" / "contracts"
    pairs = _discover_fixture_pairs(contracts_dir)
    assert len(pairs) >= 1, (
        f"No <name>.json + <name>.expected.json pairs found in {contracts_dir}. "
        "Phase A.1.6 ships at least one valid + one invalid pair; check git log."
    )


def test_pydantic_and_ajv_agree(fixture_pair):
    """Pydantic and ajv must agree with the fixture's declared expected verdict.

    Three things must align: pydantic_valid == ajv_valid == expected['valid'].
    If any two differ, the contract source-of-truth (Pydantic) and its derived
    JSON Schema (consumed by ajv) have drifted - see schemas/README.md for the
    invariant.
    """
    fixture_file, expected_file = fixture_pair
    expected = json.loads(expected_file.read_text())
    schema_name = expected["schema"]
    expected_valid = expected["valid"]

    fixture_data = json.loads(fixture_file.read_text())

    pydantic_valid = _validate_with_pydantic(fixture_data, schema_name)
    ajv_valid, ajv_stderr = _validate_with_ajv(fixture_file, schema_name)

    # All three must agree. If pydantic and ajv disagree, the contract has
    # drifted between the two languages - that's the bug this test exists to
    # catch. If they agree but neither matches expected, the fixture itself is
    # wrong (or the contract changed and we need to update fixtures).
    assert pydantic_valid == ajv_valid, (
        f"Pydantic and ajv disagree on {fixture_file.name} (schema={schema_name}): "
        f"pydantic={pydantic_valid}, ajv={ajv_valid}. "
        f"ajv stderr: {ajv_stderr!r}"
    )
    assert pydantic_valid == expected_valid, (
        f"Both validators say {fixture_file.name} is "
        f"{'valid' if pydantic_valid else 'invalid'}, but expected.json says "
        f"{'valid' if expected_valid else 'invalid'}. "
        f"Either the fixture is wrong or the contract changed."
    )
