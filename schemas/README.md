# JSON Schema files

Files in this directory are **generated** from the Pydantic models in `test_ui/contracts/ai_contract.py`.

Do not edit by hand. Regenerate with:

```bash
python scripts/export_schemas.py
```

CI verifies these files are in sync with the Pydantic source. A drift between Python and Node validation has burned us before; the only authoritative shape lives in Python, and Node's `ajv` consumes whatever Python emits.
