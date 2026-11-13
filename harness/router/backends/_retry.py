"""Exponential-backoff retry for transient cloud-backend failures.

OpenHarness "RetryingBrain" borrow (design-audit 2026-06-27). T3 cloud calls
(Anthropic / OpenAI-compatible) previously made a single HTTP request: one 429/503/504
or a transient transport hiccup dropped the whole request. This wraps them with bounded
exponential backoff.

Scope (deliberately conservative):
  - Retries ONLY 429 / 503 / 504 + httpx timeout/transport errors. 4xx auth/validation
    (400/401/403/404) raise immediately -- retrying them is pointless and hides bugs.
  - Streaming retries cover ESTABLISHMENT only: a transient failure BEFORE the first
    chunk is yielded is retried; once bytes have streamed, errors propagate (a partial
    stream cannot be safely replayed).

Non-protected helper module (backends/ is not in the protected set).
"""
import asyncio

import httpx

TRANSIENT_STATUS = frozenset({429, 503, 504})
MAX_RETRIES = 3
BASE_DELAY = 0.5  # seconds; delay = BASE_DELAY * 2**attempt -> 0.5, 1.0, 2.0


def is_transient(exc: Exception) -> bool:
    """True for retryable cloud failures: 429/503/504 or timeout/transport errors."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in TRANSIENT_STATUS
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


async def with_retries(factory, *, retries=MAX_RETRIES, base_delay=BASE_DELAY):
    """Await the 0-arg async `factory`, retrying transient failures with exponential
    backoff. Non-transient exceptions raise on the first occurrence."""
    attempt = 0
    while True:
        try:
            return await factory()
        except Exception as exc:  # noqa: BLE001 -- re-raised unless transient
            if attempt >= retries or not is_transient(exc):
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
            attempt += 1


async def retry_stream(gen_factory, *, retries=MAX_RETRIES, base_delay=BASE_DELAY):
    """Yield from a fresh async iterator produced by `gen_factory()`, retrying transient
    failures that occur BEFORE the first item is yielded (i.e. connection/establishment).
    Once a chunk has been yielded, errors propagate -- a partial stream is not replayed."""
    attempt = 0
    while True:
        agen = gen_factory()
        started = False
        try:
            async for item in agen:
                started = True
                yield item
            return
        except Exception as exc:  # noqa: BLE001
            if started or attempt >= retries or not is_transient(exc):
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
            attempt += 1
