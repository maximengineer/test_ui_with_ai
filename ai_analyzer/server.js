// AI analyzer service.
//
// Talks to OpenRouter using the OpenAI-compatible chat completions API.
// Default model: qwen/qwen3.6-plus (text+image+video input, 1M context,
// $0.325/M in / $1.95/M out — cheaper than typical hosted-provider paid
// tiers with no per-day RPD throttle). Audited against report
// 01KR1QKTTJQZJ1FJYECQ1M2W6Q where 18/20 sites errored on the prior
// provider's free-tier `RESOURCE_EXHAUSTED` quota.
//
// The OpenAI Node SDK is a drop-in client for any OpenAI-compatible
// endpoint - `baseURL` + `apiKey` is all that needs to change to swap
// providers. Image inputs use the standard `image_url` content-part
// shape (data URI), which OpenRouter forwards to the upstream model.
//
// Other infrastructure (kept from the prior provider iteration):
// - express 5 (auto-catches async route rejections)
// - ajv + ajv-formats for strict request/response validation against the
//   JSON Schemas generated from Pydantic models in test_ui/contracts/
// - Bounded payload (per-category caps + per-snippet caps) so a pathological
//   page can't blow the request size
// - Magic-byte image validation
// - Typed AIAnalysisError on every failure path (no synthetic
//   ERROR-as-success responses)

const express = require('express');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const Ajv = require('ajv').default;
const addFormats = require('ajv-formats').default;
const OpenAI = require('openai');

// =============================================================================
// Constants
// =============================================================================

// Path resolution (Phase A.1.3).
const SCHEMAS_DIR = process.env.SCHEMAS_DIR || path.resolve(__dirname, '../schemas');
const PROMPTS_DIR = process.env.PROMPTS_DIR || path.resolve(__dirname, 'prompts');

// Provider config. baseURL is hardcoded to OpenRouter; if a future migration
// targets a different OpenAI-compatible endpoint, override `AFR_AI_BASE_URL`.
const DEFAULT_BASE_URL = process.env.AFR_AI_BASE_URL || 'https://openrouter.ai/api/v1';
// Model selection. Qwen 3.6 Plus has confirmed text+image+video input on
// OpenRouter (verified via the models JSON API) and is the cheapest
// vision-capable choice in the audit. Override via AFR_AI_MODEL.
const DEFAULT_MODEL = process.env.AFR_AI_MODEL || 'qwen/qwen3.6-plus';

// Bounded payload defaults. These are conservative caps so we never blow up
// even on a pathological page; tighten later if real-data measurement (Phase
// A.1.1 follow-up) shows we need to.
//
// Body cap math: 3 images × 10 MB decoded ≈ 14 MB base64 each = ~42 MB worst-
// case body. 50 MB cap leaves headroom for the structured data + prompt + JSON
// envelope. Express enforces the body cap; the per-image cap fires inside our
// handler. The two MUST stay in agreement or one cap is dead code.
const MAX_IMAGE_DECODED_BYTES = 10 * 1024 * 1024;
const MAX_BODY_BYTES = 50 * 1024 * 1024;
const MAX_CHANGES_PER_CATEGORY = 200;
const MAX_SNIPPET_CHARS = 2000;
const PRIORITIZATION_ORDER =
  'structural HTML changes first, then content changes, then CSS/JS file changes';
const PROMPT_TEMPLATE_VALUES = {
  PRIORITIZATION_ORDER,
  MAX_CHANGES_PER_CATEGORY: String(MAX_CHANGES_PER_CATEGORY),
  MAX_SNIPPET_CHARS: String(MAX_SNIPPET_CHARS),
};

const PORT = 3000;

async function defaultInvokeProviderChatCompletion({ apiKey, baseURL, model, messages }) {
  const ai = new OpenAI({ apiKey, baseURL });
  return ai.chat.completions.create({
    model,
    messages,
  });
}

let invokeProviderChatCompletion = defaultInvokeProviderChatCompletion;

// =============================================================================
// Schema loading + ajv compile (once, at startup)
// =============================================================================

const ajv = new Ajv({ strict: true, allErrors: true });
addFormats(ajv);

let validateRequest;
let validateResponseShape;
let SCHEMA_VERSION;
let SCHEMAS_SHA256;

function loadSchemas() {
  const requestSchemaPath = path.join(SCHEMAS_DIR, 'ai_request.schema.json');
  const responseSchemaPath = path.join(SCHEMAS_DIR, 'ai_response.schema.json');

  const requestSchema = JSON.parse(fs.readFileSync(requestSchemaPath, 'utf-8'));
  const responseSchema = JSON.parse(fs.readFileSync(responseSchemaPath, 'utf-8'));

  validateRequest = ajv.compile(requestSchema);
  validateResponseShape = ajv.compile(responseSchema);

  // Pull the canonical schema_version from the request schema's default.
  // Server-side hardcoding would drift; reading from the file keeps it in
  // sync with whatever the Pydantic source emitted.
  SCHEMA_VERSION = requestSchema.properties?.schema_version?.default;
  if (!SCHEMA_VERSION) {
    throw new Error('Schema version not found in ai_request.schema.json defaults');
  }

  // Hash the schemas we actually loaded so /health can prove what's running.
  // Sort filenames for deterministic hashing across environments.
  const schemaFiles = fs.readdirSync(SCHEMAS_DIR).filter(f => f.endsWith('.schema.json')).sort();
  const concatenated = schemaFiles.map(f => fs.readFileSync(path.join(SCHEMAS_DIR, f))).join('');
  SCHEMAS_SHA256 = crypto.createHash('sha256').update(concatenated).digest('hex');

  console.log(`Loaded ${schemaFiles.length} schemas from ${SCHEMAS_DIR} (schema_version=${SCHEMA_VERSION})`);
}

// =============================================================================
// System prompt + hash (Phase A.1.5 - load from file, fail loud if missing)
// =============================================================================

let SYSTEM_PROMPT;
let PROMPT_SHA256;

function loadSystemPrompt() {
  const promptPath = path.join(PROMPTS_DIR, 'system.txt');
  // Fail loud, no fallback. Better to crash at startup than to silently serve
  // analyses with a wrong/missing prompt and then wonder why output drifted.
  if (!fs.existsSync(promptPath)) {
    throw new Error(
      `System prompt not found at ${promptPath}. ` +
      `Set PROMPTS_DIR or create the file. ` +
      `(Phase A.1.5 deliverable; see ai_analyzer/prompts/system.txt in the repo.)`
    );
  }
  const template = fs.readFileSync(promptPath, 'utf-8');
  SYSTEM_PROMPT = renderSystemPromptTemplate(template);
  if (SYSTEM_PROMPT.trim().length === 0) {
    throw new Error(`System prompt at ${promptPath} is empty.`);
  }
  PROMPT_SHA256 = crypto.createHash('sha256').update(SYSTEM_PROMPT).digest('hex');
  console.log(`Loaded system prompt from ${promptPath} (${SYSTEM_PROMPT.length} chars, sha256=${PROMPT_SHA256.slice(0, 12)}...)`);
}

// =============================================================================
// Helpers
// =============================================================================

// Inspect a base64 image: validate magic bytes, validate decoded size, return
// the detected mimeType. Returns { mimeType } on success, { error } on failure.
// Caller passes the detected mimeType so JPEG bytes aren't sent with
// 'image/png' (and vice versa).
//
// Accepted formats: PNG, JPEG, WebP. WebP is in the accepted list because
// the crawler's `compress_base64_screenshot` (test_ui/common/images.py)
// prefers WebP encoding for size/quality and writes the bytes to a path
// ending in `.png` - the file extension is a misnomer, the actual bytes
// are RIFF/WebP. The OpenAI image_url content-part accepts data URIs with
// `image/webp` natively, so we trust the magic bytes and forward the
// right mimeType.
function inspectImageBase64(b64) {
  if (!b64 || typeof b64 !== 'string') return { error: 'image is not a string' };
  let decoded;
  try {
    decoded = Buffer.from(b64, 'base64');
  } catch (e) {
    return { error: `image base64 decode failed: ${e.message}` };
  }
  if (decoded.length === 0) return { error: 'image decoded to zero bytes' };
  if (decoded.length > MAX_IMAGE_DECODED_BYTES) {
    return { error: `image exceeds ${MAX_IMAGE_DECODED_BYTES} byte cap (${decoded.length} bytes)` };
  }
  // PNG:  89 50 4E 47                                  ('\x89PNG')
  // JPEG: FF D8 FF                                     (SOI marker)
  // WebP: 52 49 46 46 ?? ?? ?? ?? 57 45 42 50          ('RIFF' .... 'WEBP')
  //       (RIFF container: bytes 0-3 = 'RIFF', 4-7 = file size LE, 8-11 = 'WEBP')
  const isPng = decoded.length >= 4
    && decoded[0] === 0x89 && decoded[1] === 0x50
    && decoded[2] === 0x4E && decoded[3] === 0x47;
  const isJpeg = decoded.length >= 3
    && decoded[0] === 0xFF && decoded[1] === 0xD8 && decoded[2] === 0xFF;
  const isWebp = decoded.length >= 12
    && decoded[0] === 0x52 && decoded[1] === 0x49
    && decoded[2] === 0x46 && decoded[3] === 0x46
    && decoded[8] === 0x57 && decoded[9] === 0x45
    && decoded[10] === 0x42 && decoded[11] === 0x50;
  if (isPng) return { mimeType: 'image/png' };
  if (isJpeg) return { mimeType: 'image/jpeg' };
  if (isWebp) return { mimeType: 'image/webp' };
  return { error: 'image is not a valid PNG, JPEG, or WebP (magic bytes mismatch)' };
}

// Truncate a code_snippet on a single change record if present.
function truncateSnippet(change) {
  if (change && typeof change.code_snippet === 'string' && change.code_snippet.length > MAX_SNIPPET_CHARS) {
    return { ...change, code_snippet: change.code_snippet.slice(0, MAX_SNIPPET_CHARS) + '\n... [TRUNCATED]' };
  }
  return change;
}

// Apply per-category caps and prioritization. Returns a copy with bounded
// `changes` arrays plus a `_truncation_report` summarizing what was dropped.
function prioritizeStructuredData(sd) {
  const report = {};
  const result = { ...sd };

  // HTML: prioritize by impact (high → medium → low), preserving relative order
  // within each tier. structure_detail records (which carry code snippets) sort
  // first within their tier since they're the most informative.
  if (sd.html_changes && Array.isArray(sd.html_changes.changes)) {
    const impactRank = { high: 0, medium: 1, low: 2, undefined: 3 };
    const typeRank = { structure_detail: 0 };
    const sorted = [...sd.html_changes.changes].sort((a, b) => {
      const ai = impactRank[a.impact] ?? 3;
      const bi = impactRank[b.impact] ?? 3;
      if (ai !== bi) return ai - bi;
      const at = typeRank[a.type] ?? 1;
      const bt = typeRank[b.type] ?? 1;
      return at - bt;
    });
    const original = sorted.length;
    const truncated = sorted.slice(0, MAX_CHANGES_PER_CATEGORY).map(truncateSnippet);
    if (original > MAX_CHANGES_PER_CATEGORY) {
      report.html_changes = { dropped: original - MAX_CHANGES_PER_CATEGORY, kept: MAX_CHANGES_PER_CATEGORY };
    }
    result.html_changes = { ...sd.html_changes, changes: truncated };
  }

  // CSS / JS: file-level only at current comparator output. Just cap the array;
  // no priority signal exists per file.
  for (const key of ['css_changes', 'js_changes']) {
    if (sd[key] && Array.isArray(sd[key].changes)) {
      const original = sd[key].changes.length;
      const truncated = sd[key].changes.slice(0, MAX_CHANGES_PER_CATEGORY);
      if (original > MAX_CHANGES_PER_CATEGORY) {
        report[key] = { dropped: original - MAX_CHANGES_PER_CATEGORY, kept: MAX_CHANGES_PER_CATEGORY };
      }
      result[key] = { ...sd[key], changes: truncated };
    }
  }

  return { bounded: result, truncationReport: report };
}

// Build the user-side prompt sent alongside the system prompt and image parts.
function buildUserPrompt(url, boundedStructuredData, truncationReport) {
  const truncationNote = Object.keys(truncationReport).length > 0
    ? `\n\nTRUNCATION APPLIED: ${JSON.stringify(truncationReport)}`
    : '';
  return `URL: ${url}

STRUCTURED DATA (JSON):
${JSON.stringify(boundedStructuredData, null, 2)}${truncationNote}

Now analyze this and respond as instructed.`;
}

// Render the checked-in prompt template using server-owned constants so
// prompt copy cannot drift from enforcement logic.
function renderSystemPromptTemplate(template) {
  let rendered = template;
  for (const [token, value] of Object.entries(PROMPT_TEMPLATE_VALUES)) {
    const marker = `{{${token}}}`;
    if (!rendered.includes(marker)) {
      throw new Error(
        `system prompt template missing required marker ${marker}`
      );
    }
    rendered = rendered.split(marker).join(value);
  }
  return rendered;
}

// OpenAI-compatible SDKs can return either:
// - string content
// - array of content parts ({ type: "text", text: "..." }, ...)
function extractAssistantText(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content
    .map(part => {
      if (!part || typeof part !== 'object') return '';
      if (typeof part.text === 'string') return part.text;
      return '';
    })
    .filter(Boolean)
    .join('\n')
    .trim();
}

// Strip markdown fences and parse the model's text into a JSON object.
// Throws on parse failure; caller wraps in AIAnalysisError.
function extractAnalysisJson(rawText) {
  if (!rawText || typeof rawText !== 'string') {
    throw new Error('AI response was empty or non-string');
  }
  const cleaned = rawText.replace(/```json/gi, '').replace(/```/g, '').trim();
  return JSON.parse(cleaned);
}

// Build a typed AIAnalysisError response. status defaults to 500; pass 400 for
// schema/validation problems. request_id may be null when the request body
// failed to parse (so we couldn't read it).
function buildErrorResponse({ request_id, error_type, retryable, details, model = null, status = 500 }) {
  return {
    body: {
      schema_version: SCHEMA_VERSION,
      result_type: 'analysis_error',
      request_id: request_id ?? null,
      model,
      prompt_sha256: PROMPT_SHA256,
      error_type,
      retryable,
      details,
    },
    status,
  };
}

function _readHeader(headers, name) {
  if (!headers) return null;
  if (typeof headers.get === 'function') {
    return headers.get(name) ?? headers.get(name.toLowerCase()) ?? null;
  }
  const direct = headers[name] ?? headers[name.toLowerCase()] ?? headers[name.toUpperCase()];
  if (Array.isArray(direct)) return direct[0] ?? null;
  if (direct != null) return direct;
  if (typeof headers === 'object') {
    const target = name.toLowerCase();
    for (const [k, v] of Object.entries(headers)) {
      if (k.toLowerCase() !== target) continue;
      if (Array.isArray(v)) return v[0] ?? null;
      return v ?? null;
    }
  }
  return null;
}

function extractRetryAfterHeader(err) {
  const header =
    _readHeader(err?.headers, 'retry-after')
    ?? _readHeader(err?.response?.headers, 'retry-after')
    ?? null;
  if (header == null) return null;
  const value = String(header).trim();
  return value.length > 0 ? value : null;
}

function _statusFromProviderError(err) {
  if (typeof err?.status === 'number') return err.status;
  if (typeof err?.response?.status === 'number') return err.response.status;
  if (typeof err?.statusCode === 'number') return err.statusCode;
  return null;
}

function classifyProviderError(err) {
  const message = err?.message ? String(err.message) : String(err);
  const status = _statusFromProviderError(err);
  const retryAfterHeader = extractRetryAfterHeader(err);

  if (err instanceof OpenAI.RateLimitError || status === 429 || /rate[- ]?limit|429/.test(message.toLowerCase())) {
    return {
      error_type: 'rate_limited',
      retryable: true,
      status: 429,
      retry_after: retryAfterHeader,
      details: `AI provider rate limit: ${message}`,
    };
  }

  if (err instanceof OpenAI.APIConnectionTimeoutError || /timed?\s*out|timeout/i.test(message)) {
    return {
      error_type: 'timeout',
      retryable: true,
      status: 504,
      retry_after: null,
      details: `AI provider timeout: ${message}`,
    };
  }

  if (err instanceof OpenAI.AuthenticationError || err instanceof OpenAI.PermissionDeniedError || status === 401 || status === 403) {
    return {
      error_type: 'config_error',
      retryable: false,
      status: 500,
      retry_after: null,
      details: `AI provider authentication/permission error: ${message}`,
    };
  }

  if (err instanceof OpenAI.BadRequestError || status === 400 || status === 404 || status === 409 || status === 422) {
    return {
      error_type: 'provider_error',
      retryable: false,
      status: 502,
      retry_after: null,
      details: `AI provider rejected request: ${message}`,
    };
  }

  if (err instanceof OpenAI.InternalServerError || (typeof status === 'number' && status >= 500)) {
    return {
      error_type: 'provider_error',
      retryable: true,
      status: 502,
      retry_after: null,
      details: `AI provider server error: ${message}`,
    };
  }

  if (err instanceof OpenAI.APIConnectionError) {
    return {
      error_type: 'provider_error',
      retryable: true,
      status: 502,
      retry_after: null,
      details: `AI provider connection error: ${message}`,
    };
  }

  return {
    error_type: 'provider_error',
    retryable: true,
    status: 502,
    retry_after: null,
    details: `AI provider error: ${message}`,
  };
}

function setProviderInvokerForTests(invoker) {
  invokeProviderChatCompletion = invoker;
}

function resetProviderInvokerForTests() {
  invokeProviderChatCompletion = defaultInvokeProviderChatCompletion;
}

// Wrap a model output payload with server-controlled metadata fields, then
// validate the assembled response against the response schema. Throws if the
// assembled response doesn't validate (which would mean the model returned a
// shape we can't honor).
function buildSuccessResponse({ request_id, model, modelOutput }) {
  const response = {
    schema_version: SCHEMA_VERSION,
    result_type: 'analysis_success',
    request_id,
    model,
    prompt_sha256: PROMPT_SHA256,
    ...modelOutput,
  };
  if (!validateResponseShape(response)) {
    const errs = (validateResponseShape.errors || []).map(e => `${e.instancePath} ${e.message}`).join('; ');
    throw new Error(`assembled response does not match ai_response schema: ${errs}`);
  }
  return response;
}

// =============================================================================
// Express app
// =============================================================================

loadSchemas();        // throws if schemas/ is missing or malformed
loadSystemPrompt();   // throws if PROMPTS_DIR/system.txt is missing or empty

const app = express();
app.use(express.json({ limit: MAX_BODY_BYTES }));

// /health endpoint. Phase A.1.5 may extend further; current shape is sufficient
// for service discovery and the prompt/schema audit trail.
app.get('/health', (req, res) => {
  res.status(200).json({
    ok: true,
    model: DEFAULT_MODEL,
    prompt_sha256: PROMPT_SHA256,
    schemas_sha256: SCHEMAS_SHA256,
    schema_version: SCHEMA_VERSION,
  });
});

// POST /api/compare - the main analysis endpoint.
async function handleCompare(req, res) {
  const apiKey = process.env.OPENROUTER_API_KEY;
  const requestIdFromBody = req.body && typeof req.body === 'object' ? req.body.request_id ?? null : null;

  // 1. API key gate.
  if (!apiKey) {
    console.error('OPENROUTER_API_KEY not set');
    const { body, status } = buildErrorResponse({
      request_id: requestIdFromBody,
      error_type: 'config_error',
      retryable: false,
      details: 'OPENROUTER_API_KEY environment variable is not set on the AI analyzer service',
      status: 500,
    });
    return res.status(status).json(body);
  }

  // 2. Schema validation (ajv against ai_request schema).
  if (!validateRequest(req.body)) {
    const errs = (validateRequest.errors || [])
      .map(e => `${e.instancePath || '<root>'} ${e.message}`)
      .join('; ');
    const { body, status } = buildErrorResponse({
      request_id: requestIdFromBody,
      error_type: 'schema_invalid',
      retryable: false,
      details: `request does not match ai_request schema: ${errs}`,
      status: 400,
    });
    return res.status(status).json(body);
  }

  const { url, request_id, structured_data, screenshots } = req.body;

  // 3. Image validation. Only inspects images that were actually provided;
  // absent screenshots are fine (the contract allows all-None). Stash the
  // detected mimeType per image so we send the right Content-Type to the
  // OpenAI-compatible endpoint.
  const validatedImages = [];  // [{ which, b64, mimeType }, ...]
  for (const [which, b64] of Object.entries(screenshots)) {
    if (b64 == null) continue;
    const inspection = inspectImageBase64(b64);
    if (inspection.error) {
      const { body, status } = buildErrorResponse({
        request_id,
        error_type: 'schema_invalid',
        retryable: false,
        details: `screenshots.${which}: ${inspection.error}`,
        status: 400,
      });
      return res.status(status).json(body);
    }
    validatedImages.push({ which, b64, mimeType: inspection.mimeType });
  }

  // 4. Apply payload bounds + build prompt.
  const { bounded, truncationReport } = prioritizeStructuredData(structured_data);
  const userPrompt = buildUserPrompt(url, bounded, truncationReport);

  // 5. Build OpenAI-compatible chat messages. The user message carries the
  // image_url content parts (data-URI'd base64) followed by the text
  // prompt. Vision-capable models pick up the images from these parts;
  // text-only models silently ignore them. The system role keeps the
  // task framing (severity rubric, JSON shape) separate from per-page
  // payload, which keeps the prompt cache warmer across calls.
  const userContent = validatedImages.map(({ b64, mimeType }) => ({
    type: 'image_url',
    image_url: { url: `data:${mimeType};base64,${b64}` },
  }));
  userContent.push({ type: 'text', text: userPrompt });
  const messages = [
    { role: 'system', content: SYSTEM_PROMPT },
    { role: 'user', content: userContent },
  ];

  // 6. Call the provider via the OpenAI-compatible chat completions API.
  let rawText;
  try {
    const response = await invokeProviderChatCompletion({
      apiKey,
      baseURL: DEFAULT_BASE_URL,
      model: DEFAULT_MODEL,
      messages,
    });
    rawText = extractAssistantText(response.choices?.[0]?.message?.content);
  } catch (err) {
    console.error(`AI call failed for request_id=${request_id}:`, err.message);
    const classified = classifyProviderError(err);
    const { body, status } = buildErrorResponse({
      request_id,
      model: DEFAULT_MODEL,
      error_type: classified.error_type,
      retryable: classified.retryable,
      details: classified.details,
      status: classified.status,
    });
    if (classified.retry_after != null) {
      res.set('Retry-After', classified.retry_after);
    }
    return res.status(status).json(body);
  }

  // 7. Parse model output.
  let modelOutput;
  try {
    modelOutput = extractAnalysisJson(rawText);
  } catch (err) {
    console.error(`Failed to parse AI response for request_id=${request_id}:`, err.message);
    const { body, status } = buildErrorResponse({
      request_id,
      model: DEFAULT_MODEL,
      error_type: 'response_invalid',
      retryable: true,  // model variability - retry might succeed
      details: `model returned non-JSON output: ${err.message}`,
      status: 502,
    });
    return res.status(status).json(body);
  }

  // 8. Assemble + validate the success response. If validation fails the model
  // returned a shape we can't honor (missing required fields, bad enums, etc.).
  let successResponse;
  try {
    successResponse = buildSuccessResponse({ request_id, model: DEFAULT_MODEL, modelOutput });
  } catch (err) {
    console.error(`Model output failed response-shape validation for request_id=${request_id}:`, err.message);
    const { body, status } = buildErrorResponse({
      request_id,
      model: DEFAULT_MODEL,
      error_type: 'response_invalid',
      retryable: true,
      details: err.message,
      status: 502,
    });
    return res.status(status).json(body);
  }

  return res.status(200).json(successResponse);
}

app.post('/api/compare', handleCompare);

// Express 5 auto-catches async route rejections, but having a final handler
// keeps unexpected errors from leaking stack traces into responses.
app.use((err, req, res, _next) => {
  const requestIdFromBody = req.body && typeof req.body === 'object' ? req.body.request_id ?? null : null;
  if (err?.type === 'entity.parse.failed') {
    const { body, status } = buildErrorResponse({
      request_id: requestIdFromBody,
      error_type: 'schema_invalid',
      retryable: false,
      details: 'request body is not valid JSON',
      status: 400,
    });
    return res.status(status).json(body);
  }
  if (err?.type === 'entity.too.large') {
    const { body, status } = buildErrorResponse({
      request_id: requestIdFromBody,
      error_type: 'schema_invalid',
      retryable: false,
      details: `request body exceeds ${MAX_BODY_BYTES} byte cap`,
      status: 413,
    });
    return res.status(status).json(body);
  }
  console.error('unhandled error:', err);
  const { body, status } = buildErrorResponse({
    request_id: null,
    error_type: 'unknown',
    retryable: false,
    details: 'unhandled server error; see service logs',
    status: 500,
  });
  return res.status(status).json(body);
});

if (require.main === module) {
  app.listen(PORT, () => {
    console.log(`AI analyzer service listening on port ${PORT}`);
    console.log(`  base_url:       ${DEFAULT_BASE_URL}`);
    console.log(`  model:          ${DEFAULT_MODEL}`);
    console.log(`  prompt_sha256:  ${PROMPT_SHA256}`);
    console.log(`  schemas_sha256: ${SCHEMAS_SHA256}`);
  });
}

module.exports = {
  app,
  handleCompare,
  renderSystemPromptTemplate,
  extractAssistantText,
  extractRetryAfterHeader,
  classifyProviderError,
  setProviderInvokerForTests,
  resetProviderInvokerForTests,
};
