"""AIClient unit tests (Phase A.4).

Exercises every retryable / non-retryable / transport-level branch of
`test_ui/report/ai_client.py:AIClient.send`. The module owns the AI HTTP
contract - typed request shape, retry behaviour, Retry-After honoring,
semaphore concurrency cap, and contract-validation of responses. These
were all extracted from the pre-A.3 god-object with zero direct test
coverage; this file fills that gap.

Sleep is patched out (`asyncio.sleep` → no-op that records its argument)
so retry tests run instantly and we can assert *what wait was chosen*
rather than just timing it.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
import respx

from test_ui.contracts.ai_contract import SCHEMA_VERSION
from test_ui.report.ai_client import AIClient


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

AI_ANALYZER_URL = "http://test.local"


@pytest.fixture
async def client():
    """A real httpx.AsyncClient - respx intercepts at the transport layer."""
    async with httpx.AsyncClient(timeout=30.0) as c:
        yield c


@pytest.fixture
def captured_sleeps(monkeypatch):
    """Patch asyncio.sleep to record wait times instead of actually sleeping.

    Returns a list that fills with each `await asyncio.sleep(x)` call's `x`.
    Lets us assert Retry-After overrides without making the test wait seconds.
    """
    waits: list[float] = []

    async def _no_sleep(seconds):
        waits.append(seconds)

    # Patch in the module under test, not asyncio globally - keeps respx /
    # pytest-asyncio internals using the real sleep.
    monkeypatch.setattr("test_ui.report.ai_client.asyncio.sleep", _no_sleep)
    return waits


def _success_body(request_id: str = "req-123") -> dict:
    """Minimal valid AIAnalysisResponse body the contract accepts."""
    return {
        "schema_version": SCHEMA_VERSION,
        "result_type": "analysis_success",
        "request_id": request_id,
        "model": "qwen/qwen3.6-plus",
        "prompt_sha256": "a" * 64,
        "overall_severity": "WARNING",
        "business_impact": "MEDIUM",
        "detailed_analysis": {
            "visual_changes": ["hero shifted"],
            "functional_impact": ["link OK"],
            "technical_correlation": ["css .hero changed"],
        },
        "recommendations": {
            "immediate_actions": [],
            "review_items": ["confirm intentional"],
            "acceptance_criteria": "design-team sign-off",
        },
        "confidence_score": 0.82,
    }


def _error_body(
    *, retryable: bool, error_type: str = "rate_limited", request_id: str = "req-123"
) -> dict:
    """Minimal valid AIAnalysisError body."""
    return {
        "schema_version": SCHEMA_VERSION,
        "result_type": "analysis_error",
        "request_id": request_id,
        "model": "qwen/qwen3.6-plus",
        "prompt_sha256": "b" * 64,
        "error_type": error_type,
        "retryable": retryable,
        "details": f"server said {error_type}",
    }


def _ai_request(url: str = "https://example.com") -> dict:
    """A valid AIAnalysisRequest dict (the wire shape AIClient.send takes)."""
    # Build via AIClient.create_request so we exercise its shape too.
    # Each "screenshot" is base64 of a 1x1 PNG-ish payload - Pydantic only
    # checks the strings exist, not that they're valid images.
    fake_b64 = base64.b64encode(b"fake-png-bytes").decode()
    structured_data = {
        "change_summary": {
            "overall_assessment": {},
            "change_categories": {},
            "affected_components": [],
            "recommendation": "",
            "ai_analysis_priority": "low",
        },
        "html_changes": {
            "changes_detected": False,
            "change_types": [],
            "changes": [],
            "summary": {},
        },
        "css_changes": {
            "changes_detected": False,
            "change_types": [],
            "files_changed": [],
            "changes": [],
            "summary": {},
        },
        "js_changes": {
            "changes_detected": False,
            "change_types": [],
            "files_changed": [],
            "changes": [],
            "summary": {},
        },
    }
    screenshots = {"baseline_b64": fake_b64, "current_b64": fake_b64}
    return AIClient.create_request(url, structured_data, screenshots)


# ---------------------------------------------------------------------------
# create_request - pure shape verification
# ---------------------------------------------------------------------------


def test_create_request_emits_valid_contract_shape():
    """Pure function: loader-shaped inputs → AIAnalysisRequest dict on the wire."""
    req = _ai_request("https://example.com/page")

    assert req["schema_version"] == SCHEMA_VERSION
    assert req["url"] == "https://example.com/page"
    assert "request_id" in req and len(req["request_id"]) >= 32  # uuid4
    # Loader's *_b64 keys are translated into the contract's plain keys.
    assert req["screenshots"]["baseline"] is not None
    assert req["screenshots"]["current"] is not None
    assert req["screenshots"]["visual_diff"] is None
    # Loader-only metadata / visual_diff_image keys should NOT leak through.
    assert "metadata" not in req["structured_data"]


def test_create_request_drops_loader_only_keys():
    """metadata + visual_diff_image are loader bookkeeping; must not hit the wire."""
    fake_b64 = base64.b64encode(b"png").decode()
    structured_data = {
        "change_summary": {
            "overall_assessment": {},
            "change_categories": {},
            "affected_components": [],
            "recommendation": "",
            "ai_analysis_priority": "low",
        },
        "html_changes": {
            "changes_detected": False,
            "change_types": [],
            "changes": [],
            "summary": {},
        },
        "css_changes": {
            "changes_detected": False,
            "change_types": [],
            "files_changed": [],
            "changes": [],
            "summary": {},
        },
        "js_changes": {
            "changes_detected": False,
            "change_types": [],
            "files_changed": [],
            "changes": [],
            "summary": {},
        },
        # Both should be silently dropped:
        "metadata": {"diffs_directory": "/tmp/foo"},
        "visual_diff_image": "/tmp/foo/visual_diff.png",
    }
    req = AIClient.create_request(
        "https://x", structured_data, {"baseline_b64": fake_b64}
    )
    assert "metadata" not in req["structured_data"]
    assert "visual_diff_image" not in req["structured_data"]


# ---------------------------------------------------------------------------
# send - happy path
# ---------------------------------------------------------------------------


async def test_send_returns_typed_success_dict_on_200(client):
    """200 + valid AIAnalysisResponse body → returned as a dict, no retry."""
    ai_client = AIClient(client=client, ai_analyzer_url=AI_ANALYZER_URL)
    req = _ai_request()

    with respx.mock(base_url=AI_ANALYZER_URL) as mock:
        route = mock.post("/api/compare").mock(
            return_value=httpx.Response(200, json=_success_body(req["request_id"]))
        )
        result = await ai_client.send(req)

    assert route.call_count == 1, "no retry should happen on success"
    assert result["result_type"] == "analysis_success"
    assert result["overall_severity"] == "WARNING"
    assert result["request_id"] == req["request_id"]


# ---------------------------------------------------------------------------
# send - retry behaviour
# ---------------------------------------------------------------------------


async def test_send_retries_on_retryable_error_then_succeeds(client, captured_sleeps):
    """First call returns retryable error → second call succeeds. Backoff used."""
    ai_client = AIClient(client=client, ai_analyzer_url=AI_ANALYZER_URL)
    req = _ai_request()

    responses = [
        httpx.Response(
            500,
            json=_error_body(
                retryable=True,
                error_type="provider_error",
                request_id=req["request_id"],
            ),
        ),
        httpx.Response(200, json=_success_body(req["request_id"])),
    ]
    with respx.mock(base_url=AI_ANALYZER_URL) as mock:
        route = mock.post("/api/compare").mock(side_effect=responses)
        result = await ai_client.send(req)

    assert route.call_count == 2
    assert result["result_type"] == "analysis_success"
    # Exponential backoff: first retry waits 2 ** 1 = 2s.
    assert captured_sleeps == [2]


async def test_send_honors_retry_after_header(client, captured_sleeps):
    """Server returns retryable 429 with Retry-After: 7 → next sleep is 7s, not exp-backoff."""
    ai_client = AIClient(client=client, ai_analyzer_url=AI_ANALYZER_URL)
    req = _ai_request()

    responses = [
        httpx.Response(
            429,
            headers={"Retry-After": "7"},
            json=_error_body(
                retryable=True, error_type="rate_limited", request_id=req["request_id"]
            ),
        ),
        httpx.Response(200, json=_success_body(req["request_id"])),
    ]
    with respx.mock(base_url=AI_ANALYZER_URL) as mock:
        mock.post("/api/compare").mock(side_effect=responses)
        result = await ai_client.send(req)

    assert result["result_type"] == "analysis_success"
    # The whole point of A.1.10: Retry-After overrides exp-backoff (which
    # would otherwise be 2 ** 1 = 2s).
    assert captured_sleeps == [7.0]


async def test_send_falls_back_to_exp_backoff_when_retry_after_unparseable(
    client, captured_sleeps
):
    """Retry-After: HTTP-date form → we don't parse it → exp-backoff used instead."""
    ai_client = AIClient(client=client, ai_analyzer_url=AI_ANALYZER_URL)
    req = _ai_request()

    responses = [
        httpx.Response(
            429,
            headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"},
            json=_error_body(
                retryable=True, error_type="rate_limited", request_id=req["request_id"]
            ),
        ),
        httpx.Response(200, json=_success_body(req["request_id"])),
    ]
    with respx.mock(base_url=AI_ANALYZER_URL) as mock:
        mock.post("/api/compare").mock(side_effect=responses)
        await ai_client.send(req)

    assert captured_sleeps == [2], "should fall back to exp-backoff (2 ** 1)"


async def test_send_returns_immediately_on_non_retryable_error(client, captured_sleeps):
    """analysis_error with retryable=False → no retry, error dict returned as-is."""
    ai_client = AIClient(client=client, ai_analyzer_url=AI_ANALYZER_URL)
    req = _ai_request()

    with respx.mock(base_url=AI_ANALYZER_URL) as mock:
        route = mock.post("/api/compare").mock(
            return_value=httpx.Response(
                400,
                json=_error_body(
                    retryable=False,
                    error_type="schema_invalid",
                    request_id=req["request_id"],
                ),
            )
        )
        result = await ai_client.send(req)

    assert route.call_count == 1, "no retry on retryable=False"
    assert captured_sleeps == [], "no sleep should happen"
    assert result["result_type"] == "analysis_error"
    assert result["error_type"] == "schema_invalid"
    assert result["retryable"] is False


async def test_send_exhausts_retries_then_returns_typed_error(client, captured_sleeps):
    """All retries fail with retryable error → last server error is returned."""
    ai_client = AIClient(client=client, ai_analyzer_url=AI_ANALYZER_URL)
    req = _ai_request()
    error_payload = _error_body(
        retryable=True, error_type="provider_error", request_id=req["request_id"]
    )

    with respx.mock(base_url=AI_ANALYZER_URL) as mock:
        route = mock.post("/api/compare").mock(
            return_value=httpx.Response(503, json=error_payload)
        )
        result = await ai_client.send(req, max_retries=3)

    assert route.call_count == 3
    # Sleeps between attempts only - N attempts means N-1 sleeps.
    assert captured_sleeps == [2, 4]
    assert result["result_type"] == "analysis_error"
    assert result["retryable"] is True


# ---------------------------------------------------------------------------
# send - transport-level failures
# ---------------------------------------------------------------------------


async def test_send_synthesizes_timeout_error_after_retries(client, captured_sleeps):
    """All attempts time out → AIClient synthesizes an AIAnalysisError(timeout)."""
    ai_client = AIClient(client=client, ai_analyzer_url=AI_ANALYZER_URL)
    req = _ai_request()

    with respx.mock(base_url=AI_ANALYZER_URL) as mock:
        mock.post("/api/compare").mock(side_effect=httpx.TimeoutException("simulated"))
        result = await ai_client.send(req, max_retries=2)

    assert result["result_type"] == "analysis_error"
    assert result["error_type"] == "timeout"
    assert result["retryable"] is True
    # Synthesized error has request_id propagated from the request.
    assert result["request_id"] == req["request_id"]


async def test_send_synthesizes_provider_error_on_transport_failure(
    client, captured_sleeps
):
    """httpx.ConnectError exhausting retries → synthesized provider_error."""
    ai_client = AIClient(client=client, ai_analyzer_url=AI_ANALYZER_URL)
    req = _ai_request()

    with respx.mock(base_url=AI_ANALYZER_URL) as mock:
        mock.post("/api/compare").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        result = await ai_client.send(req, max_retries=2)

    assert result["result_type"] == "analysis_error"
    assert result["error_type"] == "provider_error"
    assert result["retryable"] is True


# ---------------------------------------------------------------------------
# send - malformed / contract-violating responses
# ---------------------------------------------------------------------------


async def test_send_returns_response_invalid_on_non_json_body(client, captured_sleeps):
    """200 with garbage body → response_invalid, not retried (server bug, not transient)."""
    ai_client = AIClient(client=client, ai_analyzer_url=AI_ANALYZER_URL)
    req = _ai_request()

    with respx.mock(base_url=AI_ANALYZER_URL) as mock:
        route = mock.post("/api/compare").mock(
            return_value=httpx.Response(200, content=b"not json at all")
        )
        result = await ai_client.send(req)

    assert route.call_count == 1, "non-JSON body must NOT trigger retry"
    assert result["result_type"] == "analysis_error"
    assert result["error_type"] == "response_invalid"
    assert result["retryable"] is False


async def test_send_returns_response_invalid_on_schema_violation(
    client, captured_sleeps
):
    """200 with valid JSON but missing fields → response_invalid (no retry)."""
    ai_client = AIClient(client=client, ai_analyzer_url=AI_ANALYZER_URL)
    req = _ai_request()

    with respx.mock(base_url=AI_ANALYZER_URL) as mock:
        route = mock.post("/api/compare").mock(
            return_value=httpx.Response(
                200, json={"result_type": "analysis_success", "request_id": "x"}
            )
        )
        result = await ai_client.send(req)

    assert route.call_count == 1
    assert result["result_type"] == "analysis_error"
    assert result["error_type"] == "response_invalid"
    assert result["retryable"] is False


async def test_send_rejects_marker_result_types_on_the_wire(client, captured_sleeps):
    """Server returning no_changes / ai_disabled is illegal - those are client-only.

    Discriminator parses successfully (the union accepts them) but the send
    code path treats them as a server-side bug → response_invalid.
    """
    ai_client = AIClient(client=client, ai_analyzer_url=AI_ANALYZER_URL)
    req = _ai_request()

    with respx.mock(base_url=AI_ANALYZER_URL) as mock:
        mock.post("/api/compare").mock(
            return_value=httpx.Response(
                200,
                json={
                    "schema_version": SCHEMA_VERSION,
                    "result_type": "no_changes",
                    "checked_at": "02-05-2026 16:00:00",
                },
            )
        )
        result = await ai_client.send(req)

    assert result["result_type"] == "analysis_error"
    assert result["error_type"] == "response_invalid"
    assert "result_type" in result["details"]


# ---------------------------------------------------------------------------
# Retry-After parser - pure helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header,expected",
    [
        ("0", 0.0),
        ("5", 5.0),
        ("30.5", 30.5),
        ("-3", 0.0),  # clamped to >= 0
        ("Wed, 21 Oct 2099 07:28:00 GMT", None),  # HTTP-date form not supported
        ("not-a-number", None),
        ("", None),
    ],
)
def test_parse_retry_after(header, expected):
    response = httpx.Response(429, headers={"retry-after": header} if header else {})
    assert AIClient._parse_retry_after(response) == expected


def test_parse_retry_after_handles_none_response():
    assert AIClient._parse_retry_after(None) is None


# ---------------------------------------------------------------------------
# Semaphore - concurrency cap
# ---------------------------------------------------------------------------


async def test_semaphore_caps_in_flight_requests(client, captured_sleeps):
    """With concurrency=2, no more than 2 requests are in flight simultaneously.

    Strategy: route handler holds each request via an asyncio.Event until the
    test releases it, while tracking peak concurrency. Launches 5 sends in
    parallel, awaits release, asserts peak == 2.
    """
    ai_client = AIClient(client=client, ai_analyzer_url=AI_ANALYZER_URL, ai_concurrency=2)

    in_flight = 0
    peak = 0
    gate = asyncio.Event()
    saturated = asyncio.Event()  # set when in_flight first reaches the cap (2)
    enter_lock = asyncio.Lock()

    async def _gated_handler(request):
        nonlocal in_flight, peak
        async with enter_lock:
            in_flight += 1
            peak = max(peak, in_flight)
            if in_flight >= 2:
                saturated.set()
        await gate.wait()  # block until the test signals release
        async with enter_lock:
            in_flight -= 1
        body = json.loads(request.content)
        return httpx.Response(200, json=_success_body(body["request_id"]))

    with respx.mock(base_url=AI_ANALYZER_URL) as mock:
        mock.post("/api/compare").mock(side_effect=_gated_handler)

        # Fire 5 concurrent sends with distinct request_ids.
        send_tasks = [
            asyncio.create_task(ai_client.send(_ai_request(f"https://x/{i}")))
            for i in range(5)
        ]

        # Wait (with a generous timeout) for the cap to be reached, then assert
        # we're stuck there - no third request should slip through. The
        # `saturated` event fires the moment in_flight == 2; if the semaphore
        # is broken and lets a 3rd in, peak will record it.
        try:
            await asyncio.wait_for(saturated.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            gate.set()  # avoid hanging on cleanup
            for t in send_tasks:
                t.cancel()
            pytest.fail(f"never reached 2 in-flight requests; in_flight={in_flight}")

        # Give the loop several ticks so a leaky semaphore would have time to
        # let extra requests through (and bump `peak`).
        for _ in range(20):
            await asyncio.sleep(0)
        assert in_flight == 2, f"expected exactly 2 in-flight, got {in_flight}"

        gate.set()
        results = await asyncio.gather(*send_tasks)

    assert peak == 2, f"semaphore breached: peak in-flight was {peak}"
    assert all(r["result_type"] == "analysis_success" for r in results)


async def test_semaphore_floor_at_one(client):
    """ai_concurrency=0 (or negative) is floored to 1 to avoid Semaphore(0) deadlock.

    Inspects `Semaphore._value` (a private CPython attribute, stable since 3.4
    but not part of the documented API). The behavioral test above
    (test_semaphore_caps_in_flight_requests) exercises the same constraint
    via observable behavior - if `_value` ever changes, this assertion is
    cheap to update without losing real coverage.
    """
    ai_client = AIClient(client=client, ai_analyzer_url=AI_ANALYZER_URL, ai_concurrency=0)
    assert ai_client.semaphore._value == 1
