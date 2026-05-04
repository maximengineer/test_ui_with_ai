#!/usr/bin/env node
// Validate a JSON fixture file against a named schema using ajv.
//
// Phase A.1.6 deliverable. Used by tests/test_contract_smoke.py to verify
// that ajv agrees with Pydantic on whether a sample is valid. Same ajv
// configuration as ai_analyzer/server.js (strict mode + ajv-formats).
//
// Usage:
//   node validate.js <fixture-path> <schema-name>
//
//   <schema-name> is the schema base name without ".schema.json" suffix.
//   Examples: "ai_request", "ai_response", "ai_error", "no_changes", "ai_disabled".
//
// Exits 0 on success of the script (whether or not the fixture is valid).
// Exits 1 on script-level error (fixture missing, schema missing, bad args).
//
// Stdout: a single character - "V" if the fixture validates, "I" if not.
// Stderr: detailed ajv errors when invalid (handy for debugging fixtures).

const fs = require('fs');
const path = require('path');
const Ajv = require('ajv').default;
const addFormats = require('ajv-formats').default;

// Same path-resolution pattern as server.js: env var override, sensible default.
// __dirname here is ai_analyzer/scripts/, so ../../schemas resolves to <repo>/schemas.
const SCHEMAS_DIR = process.env.SCHEMAS_DIR || path.resolve(__dirname, '../../schemas');

function die(msg, code = 1) {
  process.stderr.write(`validate.js: ${msg}\n`);
  process.exit(code);
}

function main() {
  const [fixturePath, schemaName] = process.argv.slice(2);
  if (!fixturePath || !schemaName) {
    die('usage: node validate.js <fixture-path> <schema-name>');
  }

  if (!fs.existsSync(fixturePath)) {
    die(`fixture not found: ${fixturePath}`);
  }

  const schemaPath = path.join(SCHEMAS_DIR, `${schemaName}.schema.json`);
  if (!fs.existsSync(schemaPath)) {
    die(`schema not found: ${schemaPath}`);
  }

  let fixture;
  try {
    fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf-8'));
  } catch (e) {
    die(`fixture ${fixturePath} is not valid JSON: ${e.message}`);
  }

  let schema;
  try {
    schema = JSON.parse(fs.readFileSync(schemaPath, 'utf-8'));
  } catch (e) {
    die(`schema ${schemaPath} is not valid JSON: ${e.message}`);
  }

  // Match server.js exactly so smoke results predict production behavior.
  const ajv = new Ajv({ strict: true, allErrors: true });
  addFormats(ajv);

  const validate = ajv.compile(schema);
  const ok = validate(fixture);

  if (ok) {
    process.stdout.write('V');
  } else {
    process.stdout.write('I');
    // Detailed errors go to stderr so the V/I result on stdout stays parseable.
    if (validate.errors) {
      for (const err of validate.errors) {
        process.stderr.write(`  ${err.instancePath || '<root>'} ${err.message}\n`);
      }
    }
  }
  // Don't call process.exit() - it can truncate buffered stdout writes per
  // Node's documented behavior. Let the process exit naturally (clean flush
  // first). exitCode defaults to 0 if everything ran to completion.
}

main();
