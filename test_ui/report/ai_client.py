"""HTTP client for the AI analyzer service (Phase A.3 split).

Wraps the POST /api/compare contract. Owns:
- The httpx.AsyncClient (passed in by the CLI; this class never closes it).
- A semaphore bounding in-flight requests (configurable via
  AFR_AI_CONCURRENCY) — prevents quota exhaustion when many URLs run
  concurrently.
- The retry loop, which honors HTTP 429 Retry-After when present and falls
  back to exponential backoff otherwise.
- Contract validation: every server reply is routed through the
  AnalysisOutput discriminated union so unexpected shapes become typed
  errors rather than KeyErrors at the call site.

Transport-level failures (no parseable body) are wrapped in a synthesized
AIAnalysisError so the caller always gets a well-shaped result.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
from loguru import logger
from pydantic import TypeAdapter, ValidationError

from ..config import settings
from ..contracts.ai_contract import (
    AIAnalysisError,
    AIAnalysisRequest,
    AIAnalysisResponse,
    AnalysisOutput,
    Screenshots,
    StructuredData,
)


class AIClient:
    """POST AIAnalysisRequest payloads to the analyzer; return typed results."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        gemini_url: str,
        ai_concurrency: int | None = None,
    ):
        self.client = client
        self.gemini_url = gemini_url
        # Floor at 1: a Semaphore(0) would deadlock.
        concurrency = (
            ai_concurrency if ai_concurrency is not None else settings.ai_concurrency
        )
        self.semaphore = asyncio.Semaphore(max(1, concurrency))

    @staticmethod
    def create_request(
        url: str, structured_data: dict[str, Any], screenshots: dict[str, Any]
    ) -> dict[str, Any]:
        """Build an AIAnalysisRequest dict from loader-shaped inputs.

        The loader's screenshots dict uses *_b64 / *_path keys; only the b64
        bodies belong on the wire. Loader-only structured-data keys
        (metadata, visual_diff_image path) are dropped — the contract only
        carries the four typed change blocks.
        """
        sshots = Screenshots(
            baseline=screenshots.get("baseline_b64"),
            current=screenshots.get("current_b64"),
            visual_diff=screenshots.get("visual_diff_b64"),
        )
        sd = StructuredData(
            change_summary=structured_data.get("change_summary", {}),
            html_changes=structured_data.get("html_changes", {}),
            css_changes=structured_data.get("css_changes", {}),
            js_changes=structured_data.get("js_changes", {}),
        )
        request = AIAnalysisRequest(
            request_id=str(uuid.uuid4()),
            url=url,
            structured_data=sd,
            screenshots=sshots,
        )
        return request.model_dump(mode="json")

    async def send(
        self, ai_request: dict[str, Any], max_retries: int = 3
    ) -> dict[str, Any]:
        """POST the request; retry on retryable errors; return typed response as dict."""
        url = ai_request.get("url", "unknown")
        request_id = ai_request.get("request_id")
        last_error: Exception | None = None
        # Override for the next iteration's sleep, set when we observe a
        # Retry-After header on a retryable response. Reset after consumption.
        next_wait_override: float | None = None

        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    if next_wait_override is not None:
                        wait_time = next_wait_override
                        backoff_source = "Retry-After"
                    else:
                        wait_time = 2**attempt
                        backoff_source = "exp-backoff"
                    next_wait_override = None
                    logger.info(
                        f"Retrying AI analysis for {url} (attempt {attempt + 1}/{max_retries}) "
                        f"after {wait_time:.1f}s ({backoff_source})"
                    )
                    await asyncio.sleep(wait_time)
                else:
                    logger.info(
                        f"Sending AIAnalysisRequest for {url} (request_id={request_id})"
                    )

                # Acquire the semaphore around the network call only. Sleeps
                # don't hold the slot — concurrent retries should be bounded
                # by in-flight requests, not by total elapsed time.
                async with self.semaphore:
                    response = await self.client.post(
                        f"{self.gemini_url}/api/compare",
                        json=ai_request,
                        timeout=120.0,
                    )

                # Don't raise_for_status — server returns typed error bodies
                # on 4xx/5xx and we want to parse them.
                try:
                    body = response.json()
                except json.JSONDecodeError as e:
                    logger.error(
                        f"AI analyzer returned non-JSON body for {url}: {response.text[:200]}"
                    )
                    return self._synthesize_error(
                        request_id=request_id,
                        error_type="response_invalid",
                        retryable=False,
                        details=f"AI analyzer returned non-JSON body: {e}",
                    )

                try:
                    parsed = TypeAdapter(AnalysisOutput).validate_python(body)
                except ValidationError as e:
                    logger.error(
                        f"AI analyzer body did not match contract for {url}: {e}"
                    )
                    return self._synthesize_error(
                        request_id=request_id,
                        error_type="response_invalid",
                        retryable=False,
                        details=f"AI analyzer body did not match AnalysisOutput shape: {e}"[
                            :1000
                        ],
                    )

                if response.is_success and isinstance(parsed, AIAnalysisResponse):
                    logger.info(
                        f"AI analysis OK for {url} → {parsed.overall_severity} "
                        f"(model={parsed.model}, prompt={parsed.prompt_sha256[:8]})"
                    )
                    return parsed.model_dump(mode="json")

                if isinstance(parsed, AIAnalysisError):
                    logger.error(
                        f"AI analyzer error for {url}: {parsed.error_type} "
                        f"({parsed.details[:120]})"
                    )
                    if parsed.retryable and attempt < max_retries - 1:
                        next_wait_override = self._parse_retry_after(response)
                        last_error = RuntimeError(parsed.details)
                        continue
                    return parsed.model_dump(mode="json")

                # Server returned a marker type (no_changes / ai_disabled) on
                # the wire — that should never happen; the server doesn't own
                # those semantics.
                logger.error(
                    f"AI analyzer returned unexpected result_type for {url}: {parsed.result_type}"
                )
                return self._synthesize_error(
                    request_id=request_id,
                    error_type="response_invalid",
                    retryable=False,
                    details=f"server returned unexpected result_type: {parsed.result_type}",
                )

            except httpx.TimeoutException as e:
                last_error = e
                logger.error(
                    f"AI analyzer timeout for {url} attempt {attempt + 1}: {e}"
                )
                if attempt == max_retries - 1:
                    break

            except httpx.HTTPError as e:
                last_error = e
                logger.error(
                    f"AI analyzer transport error for {url} attempt {attempt + 1}: {e}"
                )
                if attempt == max_retries - 1:
                    break

            except Exception as e:
                last_error = e
                logger.error(
                    f"Unexpected error during AI analysis for {url} attempt {attempt + 1}: {e}"
                )
                if attempt == max_retries - 1:
                    break

        # Exhausted retries with a transport-level error (server never
        # responded with a parseable body). Synthesize an AIAnalysisError so
        # the caller always gets a typed result.
        err_type = (
            "timeout"
            if isinstance(last_error, httpx.TimeoutException)
            else "provider_error"
        )
        return self._synthesize_error(
            request_id=request_id,
            error_type=err_type,
            retryable=True,
            details=f"All {max_retries} attempts failed: {type(last_error).__name__}: {str(last_error)[:500]}",
        )

    @staticmethod
    def _parse_retry_after(response) -> float | None:
        """Read the Retry-After header. Returns seconds, or None if absent/unparseable.

        Supports the integer-seconds form (`Retry-After: 30`). Doesn't parse the
        HTTP-date form (`Retry-After: Wed, 21 Oct 2015 07:28:00 GMT`) — rare in
        practice; falling back to exponential backoff is acceptable.
        """
        if response is None:
            return None
        header = response.headers.get("retry-after")
        if not header:
            return None
        try:
            seconds = float(header)
        except ValueError:
            return None
        return max(0.0, seconds)

    @staticmethod
    def _synthesize_error(
        *, request_id, error_type, retryable, details, model=None
    ) -> dict[str, Any]:
        """Build an AIAnalysisError dict for transport-level failures.

        Used when the server didn't respond with a parseable typed body. For
        server-returned errors (typed AIAnalysisError on the wire), we return
        the parsed body directly — this helper only manufactures the error
        envelope ourselves.
        """
        return AIAnalysisError(
            request_id=request_id,
            model=model,
            error_type=error_type,
            retryable=retryable,
            details=details,
        ).model_dump(mode="json")


__all__ = ["AIClient"]
