# Cross-language schema contract fixtures

Used by `tests/test_contract_smoke.py` (Phase A.1.6) and the future Phase A.4 contract matrix to verify Pydantic and ajv agree on the same input.

## Layout

Each scenario is a pair:

- `<scenario>.json` — the JSON to validate (request / response / etc.).
- `<scenario>.expected.json` — `{"schema": "<name>", "valid": true|false, "note": "..."}`. Names match the schema basename without `.schema.json` (e.g. `ai_request`, `ai_response`, `ai_error`, `no_changes`, `ai_disabled`).

The `note` field is human-only — explains *why* the fixture is valid or invalid.

## Adding a fixture

1. Add `<scenario>.json` and `<scenario>.expected.json`.
2. Run `make test`. The smoke test auto-discovers any matching pair.
3. Both Pydantic (via the model imports in `test_ui/contracts/ai_contract.py`) and ajv (via `ai_analyzer/scripts/validate.js`) must agree with the expected verdict, or the test fails.

## Why this exists

Pydantic-emitted JSON Schema and ajv strict mode can disagree on edge cases — `format` validators, `null` vs `Optional`, `additionalProperties` semantics, `$defs` resolution. Catching that disagreement here, before code depends on the contract, is much cheaper than catching it later when production responses start failing validation on one side or the other.
