"""Serialize in-flight requests to the FLM NPU endpoint (R-18 mitigation).

WHY
---
FLM v0.9.45 on Forge double-frees and dies when a client disconnects during a
MULTI-CHUNK prefill while another request is queued. Root-caused and reproduced
2026-08-13: 6 of 6 crashes carry the same prelude, and every one contains

    [*] NPU busy, request queued (N/10): POST /v1/chat/completions

Remove the queued state and the trigger condition cannot arise. The XDNA2 NPU is a
SINGLE physical accelerator that cannot execute two requests in parallel anyway --
FLM already queues them internally -- so serializing here costs no throughput. It
only moves the wait from inside FLM to inside the Router, which is strictly better:
a caller that gives up while waiting for the lock never opens a request that can be
canceled mid-prefill.

WHY A SHARED MODULE AND NOT A LOCK IN ONE BACKEND
-------------------------------------------------
FOUR independent code paths reach the same physical NPU at 192.168.x.50:8003:

    npu        tier  type=ollama         -> backends/ollama.py
    npu-tools  tier  type=openai_compat  -> backends/openai_compat.py   (provider flm)
    classifier (via classifier.py)       -> npu_classifier.py        (direct FLM_URL)
    classifier (via main.py:214)         -> npu_classifier_shadow.py (direct FLM_URL)

v338 said THREE and gated only those, missing npu_classifier_shadow -- which is the one
main.py actually imports, i.e. the live hot path. Verification caught it (four concurrent
"NPU busy, request queued" and NRestarts 7 -> 8). BEFORE ADDING A NEW FLM CALLER, add it
here: an ungated caller silently re-opens the crash.

A lock inside any ONE of them would leave the other two racing it, which is exactly
the cross-path concurrency that produces the queued state. The gate is therefore keyed
on the backend's host:port, so all three share one lock per endpoint.

SCOPE: only hosts in BIFROST_NPU_SERIALIZE_HOSTS are gated. Cloud providers share the
openai_compat code path, handle concurrency fine, and must NOT be serialized.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse

log = logging.getLogger("bifrost.npu_gate")

# host:port entries, comma-separated. Default is the Forge FLM NPU endpoint.
# Set to empty to disable the gate entirely (a deliberate, greppable off-switch).
NPU_SERIALIZE_HOSTS = {
    h.strip() for h in os.getenv("BIFROST_NPU_SERIALIZE_HOSTS", "192.168.x.50:8003").split(",")
    if h.strip()
}

_LOCKS: dict[str, asyncio.Lock] = {}


class NpuBusy(TimeoutError):
    """Could not acquire the NPU within the caller's budget. Subclasses TimeoutError so
    existing `except TimeoutError` / `except Exception` fallbacks treat it as a normal
    backend timeout rather than a new failure mode."""


def _endpoint(url: str) -> str | None:
    """Return 'host:port' if `url` targets a gated NPU endpoint, else None."""
    try:
        netloc = urlparse(url).netloc
    except Exception:  # noqa: BLE001 -- a malformed URL must never break dispatch
        return None
    return netloc if netloc in NPU_SERIALIZE_HOSTS else None


def is_gated(url: str) -> bool:
    return _endpoint(url) is not None


@asynccontextmanager
async def npu_serialized(url: str, wait_timeout: float | None = None):
    """Hold the NPU lock for the duration of the block, if `url` is a gated endpoint.

    A NO-OP for every other URL, so wrapping a shared code path (openai_compat serves
    both FLM and the cloud providers) is safe.

    `wait_timeout` bounds how long to wait for the lock. On expiry it raises NpuBusy
    WITHOUT sending the request -- deliberately: a caller that has already burned its
    budget waiting must not then open a connection it is about to abandon, because
    abandoning it mid-prefill is the exact crash this gate exists to prevent.
    """
    ep = _endpoint(url)
    if ep is None:
        yield False
        return

    lock = _LOCKS.get(ep)
    if lock is None:
        lock = _LOCKS[ep] = asyncio.Lock()

    contended = lock.locked()
    if contended:
        log.debug("npu_gate: %s busy, waiting (timeout=%s)", ep, wait_timeout)
    try:
        if wait_timeout is None:
            await lock.acquire()
        else:
            await asyncio.wait_for(lock.acquire(), timeout=wait_timeout)
    except asyncio.TimeoutError:
        log.info("npu_gate: %s still busy after %.2fs -- refusing to queue at the backend",
                 ep, wait_timeout)
        raise NpuBusy("NPU endpoint %s busy for more than %.2fs" % (ep, wait_timeout)) from None

    try:
        yield True
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# R-18 residual (#94): surviving a canceled CALLER
# ---------------------------------------------------------------------------
#
# Serializing removed the "queued" half of the crash condition. It does not touch the
# other half: CANCELLATION. v340 (cff2574) shielded the one caller that canceled itself
# (classifier.py's outer wait_for), and its verification deliberately used no client
# aborts, so this path was left open and tracked as #94.
#
# The open path is an EXTERNAL abort. A client hangs up on the Router, starlette cancels
# the request handler, the cancellation propagates into whatever backend call that handler
# is awaiting, httpx tears down the connection mid-prefill, and FLM double-frees. The
# Router is again the client that disconnects -- just on someone else's behalf this time.
#
# Why a plain `shield(client.post(...))` is NOT enough, and would look like it worked:
# the httpx.AsyncClient is normally created by an `async with` in the caller's frame. Shield
# the post alone and the CancelledError still unwinds that frame, `__aexit__` closes the
# connection pool, and the "shielded" request dies anyway -- with the shield sitting in the
# source looking like protection. The whole unit (lock + client + request) has to run inside
# the shielded task. classifier.py already records the same shape: collecting a bare task
# mid-flight closes the socket, which is the very disconnect being avoided (the v97 lesson).
#
# The lock must also be acquired INSIDE the task, not around the shield. Released on the
# caller's cancellation instead, the next request would start while this one is still
# prefilling -- re-creating the queued state npu_serialized exists to remove, so the two
# halves of the fix would cancel out.

_INFLIGHT: set[asyncio.Task] = set()


def _drain_abandoned(task: asyncio.Task) -> None:
    """Retrieve the outcome of a request whose awaiter has gone away.

    An abandoned task whose exception is never retrieved is reported by asyncio at GC
    time, detached from the event that caused it -- it surfaces as an unexplained error
    class minutes later, in a log line naming no request. Retrieve it here and say plainly
    that it belonged to an abandoned call.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.info("npu_gate: abandoned NPU request finished with %s: %s",
                 type(exc).__name__, exc)


async def npu_uncancellable(url: str, make_coro, *, wait_timeout: float | None = None):
    """Run `make_coro()` so that canceling the CALLER stops us waiting without killing it.

    A NO-OP for every non-gated URL: cloud providers share these code paths and must keep
    their ordinary cancellation semantics, where dropping a request the client abandoned is
    exactly right. It is wrong only here, because for FLM v0.9.45 a cancellation mid-prefill
    is not a dropped request, it is a dead service.

    On caller cancellation this raises CancelledError as usual -- the caller is NOT made to
    wait. The request simply outlives it and its answer is discarded. `CancelledError` is a
    BaseException, so `with_retries` (which catches `Exception`) propagates it rather than
    treating the abort as a transient failure worth re-sending.

    `make_coro` must be a 0-arg callable returning a fresh coroutine, and must create any
    HTTP client INSIDE that coroutine -- see the module note above for why a client owned by
    the caller's frame defeats the shield.
    """
    if _endpoint(url) is None:
        return await make_coro()

    async def _held():
        async with npu_serialized(url, wait_timeout=wait_timeout):
            return await make_coro()

    task = asyncio.ensure_future(_held())
    # Retain a strong ref: a bare task can be garbage-collected before it finishes, which
    # closes its socket -- the disconnect this function exists to prevent.
    _INFLIGHT.add(task)
    task.add_done_callback(_INFLIGHT.discard)
    task.add_done_callback(_drain_abandoned)
    return await asyncio.shield(task)
