const test = require('node:test');
const assert = require('node:assert/strict');
const OpenAI = require('openai');

const {
  handleCompare,
  setProviderInvokerForTests,
  resetProviderInvokerForTests,
} = require('../server');

const ONE_BY_ONE_PNG_B64 =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQImWNgYGD4DwABBAEAU66P7QAAAABJRU5ErkJggg==';

function buildValidRequest(requestId) {
  return {
    request_id: requestId,
    url: 'https://example.com',
    structured_data: {
      change_summary: {
        overall_assessment: {},
        change_categories: {},
        affected_components: [],
        recommendation: '',
        ai_analysis_priority: 'low',
      },
      html_changes: {
        changes_detected: false,
        change_types: [],
        changes: [],
        summary: {},
      },
      css_changes: {
        changes_detected: false,
        change_types: [],
        files_changed: [],
        changes: [],
        summary: {},
      },
      js_changes: {
        changes_detected: false,
        change_types: [],
        files_changed: [],
        changes: [],
        summary: {},
      },
    },
    screenshots: {
      baseline: ONE_BY_ONE_PNG_B64,
      current: null,
      visual_diff: null,
    },
  };
}

function createMockResponse() {
  return {
    statusCode: null,
    headers: new Map(),
    body: null,
    set(name, value) {
      this.headers.set(String(name).toLowerCase(), String(value));
      return this;
    },
    status(code) {
      this.statusCode = code;
      return this;
    },
    json(payload) {
      this.body = payload;
      return this;
    },
  };
}

test('POST /api/compare handler forwards 429 + Retry-After from provider', async () => {
  const previousApiKey = process.env.OPENROUTER_API_KEY;
  process.env.OPENROUTER_API_KEY = 'test-key';

  setProviderInvokerForTests(async () => {
    throw new OpenAI.RateLimitError(
      429,
      { message: 'quota exceeded' },
      'quota exceeded',
      { get: name => (name === 'retry-after' ? '17' : null) }
    );
  });

  const req = { body: buildValidRequest('req-route-429') };
  const res = createMockResponse();
  try {
    await handleCompare(req, res);

    assert.equal(res.statusCode, 429);
    assert.equal(res.headers.get('retry-after'), '17');
    assert.equal(res.body.result_type, 'analysis_error');
    assert.equal(res.body.request_id, 'req-route-429');
    assert.equal(res.body.error_type, 'rate_limited');
    assert.equal(res.body.retryable, true);
  } finally {
    resetProviderInvokerForTests();
    if (previousApiKey === undefined) {
      delete process.env.OPENROUTER_API_KEY;
    } else {
      process.env.OPENROUTER_API_KEY = previousApiKey;
    }
  }
});
