const test = require('node:test');
const assert = require('node:assert/strict');
const OpenAI = require('openai');

const {
  renderSystemPromptTemplate,
  extractAssistantText,
  extractRetryAfterHeader,
  classifyProviderError,
} = require('../server');

test('renderSystemPromptTemplate replaces all required markers', () => {
  const template =
    'Order={{PRIORITIZATION_ORDER}} max={{MAX_CHANGES_PER_CATEGORY}} snippet={{MAX_SNIPPET_CHARS}}';
  const rendered = renderSystemPromptTemplate(template);
  assert.match(rendered, /structural HTML changes first/);
  assert.match(rendered, /max=200/);
  assert.match(rendered, /snippet=2000/);
});

test('renderSystemPromptTemplate fails when a required marker is missing', () => {
  assert.throws(
    () => renderSystemPromptTemplate('missing markers'),
    /missing required marker/
  );
});

test('extractAssistantText handles string and content-part array forms', () => {
  assert.equal(extractAssistantText('plain text'), 'plain text');

  const fromParts = extractAssistantText([
    { type: 'text', text: 'line 1' },
    { type: 'image_url', image_url: { url: 'data:image/png;base64,AA==' } },
    { type: 'text', text: 'line 2' },
  ]);
  assert.equal(fromParts, 'line 1\nline 2');
  assert.equal(extractAssistantText({ type: 'text', text: 'ignored object' }), '');
});

test('extractRetryAfterHeader reads retry-after across header shapes', () => {
  assert.equal(
    extractRetryAfterHeader({
      headers: { get: name => (name === 'retry-after' ? '12' : null) },
    }),
    '12'
  );
  assert.equal(
    extractRetryAfterHeader({
      response: { headers: { 'retry-after': '7' } },
    }),
    '7'
  );
  assert.equal(
    extractRetryAfterHeader({
      response: { headers: { 'Retry-After': ['9'] } },
    }),
    '9'
  );
  assert.equal(extractRetryAfterHeader({}), null);
});

test('classifyProviderError maps rate limits and forwards Retry-After', () => {
  const err = new OpenAI.RateLimitError(
    429,
    { message: 'quota exceeded' },
    'quota exceeded',
    { get: name => (name === 'retry-after' ? '13' : null) }
  );

  const out = classifyProviderError(err);
  assert.equal(out.error_type, 'rate_limited');
  assert.equal(out.retryable, true);
  assert.equal(out.status, 429);
  assert.equal(out.retry_after, '13');
});

test('classifyProviderError maps auth errors to non-retryable config_error', () => {
  const err = new OpenAI.AuthenticationError(
    401,
    { message: 'invalid key' },
    'invalid key',
    { get: () => null }
  );

  const out = classifyProviderError(err);
  assert.equal(out.error_type, 'config_error');
  assert.equal(out.retryable, false);
  assert.equal(out.status, 500);
});

test('classifyProviderError maps timeout and bad-request variants correctly', () => {
  const timeoutErr = new OpenAI.APIConnectionTimeoutError({
    message: 'Request timed out.',
  });
  const timeoutOut = classifyProviderError(timeoutErr);
  assert.equal(timeoutOut.error_type, 'timeout');
  assert.equal(timeoutOut.retryable, true);
  assert.equal(timeoutOut.status, 504);

  const badReqErr = new OpenAI.BadRequestError(
    400,
    { message: 'bad payload' },
    'bad payload',
    { get: () => null }
  );
  const badReqOut = classifyProviderError(badReqErr);
  assert.equal(badReqOut.error_type, 'provider_error');
  assert.equal(badReqOut.retryable, false);
  assert.equal(badReqOut.status, 502);
});
