"""Shared free-tier budget + per-tier message/token counter for the T3 free cloud tiers.

APPLIED, and wired. Measured 2026-09-28: main.py:1170 and subtask_graph.py:1032 both do
`from free_tier_budget import get_budget as _get_free_budget`, and main.py:1251 calls
check(). This line read "STAGED 2026-09-07, not applied" until 2026-09-28 -- true when it
was written, false once the call sites landed, and nobody updated it.

PROTECTED BY TRANSITIVITY (c2). main.py is rule_1 and imports this module, so anything
reachable from that control flow is rule_1 -- including check(). Full ceremony applies to
changes here. This line previously asserted the opposite; that was accurate only while the
module was unwired, and a reader who trusted it would have edited a protected file without
ceremony. Run `git check-attr -a` and take a preimage hash before any edit.

WHY A *SHARED* BUDGET AND NOT FIVE COUNTERS
-------------------------------------------
OpenRouter's free limits are per ACCOUNT, not per model: ~20 requests/minute and ~1,000
requests/day (50/day if the account never purchased credits -- ours reports
is_free_tier=false, so we are above that floor). Five tiers each holding "their own"
1,000/day budget would authorize 5,000 and overrun the real cap fivefold. There is exactly
one bucket, and per-tier numbers are ATTRIBUTION within it, not separate allowances.

WHAT ACTUALLY BINDS -- measured 2026-09-07, not assumed
-------------------------------------------------------
Fleet demand: busiest cloud day ever 46 requests; peak 60s cloud burst 5. Against 1,000/day
that is ~20x headroom, so the account cap is NOT the binding constraint.

What *is* binding is per-model upstream contention. In a 6-probe run,
poolside/laguna-s-2.1:free returned HTTP 429 on 5 of 6 calls -- at roughly 1 request every
30 seconds, nowhere near 20/min. Across 36 probe calls: 14% HTTP 429 and 11% HTTP 200
carrying an error object instead of a completion. So the counter's real job is
per-model cooldown and failover, with the account bucket as a backstop.

Counts are read from the telemetry DB (inference_events), which the router already writes
per call, so the daily figure survives a restart with no new persistent state.
CAVEAT: that DB only sees calls THIS router made. If the same OpenRouter key is used by
anything else, the daily count under-reads and the cap is optimistic.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field

# Headroom below the published caps: a counter that aims exactly at the limit hits it.
DAILY_CAP = int(os.getenv("BIFROST_FREE_DAILY_CAP", "950"))      # published 1000
PER_MINUTE_CAP = int(os.getenv("BIFROST_FREE_MINUTE_CAP", "18"))  # published 20
COOLDOWN_S = float(os.getenv("BIFROST_FREE_COOLDOWN_S", "300"))

# Ability-ranked failover, from the 2026-09-07 bench (6 probes each, max_tokens 1500).
# Order is deliberate: first entry is the measured best for the role, later entries are
# fallbacks used only while an earlier one is cooling down.
ROLE_CANDIDATES = {
    "assembly":   ["dots-studio/dots-3-note-preview:free",           # 6/6, median 8.6s
                   "nvidia/nemotron-3-super-120b-a12b:free"],        # 5/6, median 14.5s
    "synthesis":  ["cohere/north-mini-code:free",                    # 6/6, median 13.2s
                   "dots-studio/dots-3-note-preview:free"],
    "evaluation": ["nvidia/nemotron-3-super-120b-a12b:free",         # 5/6, median 14.5s
                   "nvidia/nemotron-3-ultra-550b-a55b:free"],        # 4/6, median 71.9s
    "overflow":   ["nvidia/nemotron-3.5-lightning:free",             # 6/6, median 60.9s
                   "dots-studio/dots-3-note-preview:free"],
    "multimodal": ["nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"],  # 5/6, median 4.8s
}

# Deliberately NOT in any list: poolside/laguna-s-2.1:free (1/6 then 2/6, HTTP 429
# throughout, across two separate runs) and google/gemma-4-*-it:free (429 on every
# attempt). Free and reachable-once is not the same as usable in a sequence.


@dataclass
class Decision:
    allow: bool
    reason: str
    model: str | None = None
    retry_after_s: float | None = None


@dataclass
class TierCounter:
    messages: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    reasoning_tokens: int = 0
    http_429: int = 0
    empty_200: int = 0
    errors: int = 0
    capped: int = 0          # finish_reason == "length": the answer was truncated
    starved: int = 0         # returned zero characters despite spending tokens
    latency_ms: list = field(default_factory=list)


class FreeTierBudget:
    """One shared account bucket, per-tier attribution, per-model cooldown."""

    def __init__(self, db_path: str | None = None):
        self._lock = threading.Lock()
        self._db = db_path or os.getenv(
            "BIFROST_TELEMETRY_DB", "/app/logs/v120-telemetry/inference_events.db")
        self._minute = deque()              # timestamps of calls in the last 60s
        self._cooldown: dict[str, float] = {}   # model -> epoch when it may be used again
        self._tiers: dict[str, TierCounter] = {}
        self._day_key = None
        self._day_base = 0                 # calls already recorded in the DB today

    # -- daily count comes from telemetry, so a restart does not reset the budget ------
    def _today_from_db(self) -> int:
        try:
            con = sqlite3.connect(self._db, timeout=5)
            try:
                row = con.execute(
                    "SELECT COUNT(*) FROM inference_events "
                    "WHERE tier_used LIKE '3-%' "
                    "AND date(timestamp,'unixepoch','localtime') = date('now','localtime')"
                ).fetchone()
                return int(row[0]) if row else 0
            finally:
                con.close()
        except Exception:
            # An unreadable DB must not silently authorize unlimited spend, but it also
            # must not wedge the router. Report 0 and let the per-minute cap hold.
            return 0

    def _roll_day(self) -> None:
        key = time.strftime("%Y-%m-%d")
        if key != self._day_key:
            self._day_key = key
            self._day_base = self._today_from_db()
            self._tiers.clear()

    def _used_today(self) -> int:
        return self._day_base + sum(c.messages for c in self._tiers.values())

    # -- the pre-call gate -------------------------------------------------------------
    def check(self, tier: str, role: str | None = None) -> Decision:
        now = time.time()
        with self._lock:
            self._roll_day()
            while self._minute and now - self._minute[0] > 60:
                self._minute.popleft()

            if self._used_today() >= DAILY_CAP:
                return Decision(False, "shared daily free budget exhausted (%d/%d)"
                                % (self._used_today(), DAILY_CAP))
            if len(self._minute) >= PER_MINUTE_CAP:
                oldest = self._minute[0]
                # L8: computed ONCE and used for both the reason and the field, so the number
                # the caller is told and the number it can read cannot drift apart.
                _retry = max(0.0, 60 - (now - oldest))
                return Decision(False, "shared per-minute free budget (%d/%d); "
                                       "soonest recovery in %ds"
                                % (len(self._minute), PER_MINUTE_CAP, round(_retry)),
                                retry_after_s=_retry)

            candidates = ROLE_CANDIDATES.get(role or "", [])
            if not candidates:
                return Decision(True, "no role candidates configured; caller's model stands")
            for model in candidates:
                until = self._cooldown.get(model, 0)
                if until <= now:
                    return Decision(True, "ok", model=model)
            soonest = min(self._cooldown[m] for m in candidates)
            # L8: as above -- one computation, two surfaces.
            _retry = max(0.0, soonest - now)
            return Decision(False, "all %d candidates for role %r cooling down; "
                                   "soonest recovery in %ds"
                            % (len(candidates), role, round(_retry)),
                            retry_after_s=_retry)

    # -- outcome recording -------------------------------------------------------------
    def _tier(self, tier: str) -> TierCounter:
        return self._tiers.setdefault(tier, TierCounter())

    def record_success(self, tier: str, model: str, tokens_in: int = 0, tokens_out: int = 0,
                       reasoning_tokens: int = 0, latency_ms: float = 0.0,
                       finish_reason: str | None = None, chars: int | None = None) -> None:
        with self._lock:
            self._roll_day()
            self._minute.append(time.time())
            c = self._tier(tier)
            c.messages += 1
            c.tokens_in += int(tokens_in or 0)
            c.tokens_out += int(tokens_out or 0)
            c.reasoning_tokens += int(reasoning_tokens or 0)
            c.latency_ms.append(float(latency_ms or 0.0))
            if finish_reason == "length":
                c.capped += 1
            # A reasoning model can return finish_reason "stop" with ZERO characters after
            # spending its whole budget on reasoning -- observed on laguna-s-2.1 (179
            # reasoning tokens, 0 chars, finish="stop"). Testing only for "length" misses it.
            if chars is not None and chars == 0 and (tokens_out or reasoning_tokens):
                c.starved += 1
            self._cooldown.pop(model, None)

    def record_429(self, tier: str, model: str) -> None:
        with self._lock:
            self._roll_day()
            self._minute.append(time.time())
            self._tier(tier).http_429 += 1
            self._cooldown[model] = time.time() + COOLDOWN_S

    def record_empty_200(self, tier: str, model: str) -> None:
        """HTTP 200 whose body carries {'error': ...} and no 'choices' key.

        Seen on 11% of probe calls, all of them nvidia-provider models. A client that
        checks the status code alone reads this as a successful empty completion.
        """
        with self._lock:
            self._roll_day()
            self._minute.append(time.time())
            self._tier(tier).empty_200 += 1
            self._cooldown[model] = time.time() + COOLDOWN_S / 5

    # -- adapters for the router's dispatch path ---------------------------------------
    def record_response(self, tier: str, model: str, result, latency_ms: float = 0.0) -> None:
        """Record whatever a backend actually returned.

        Handles three shapes seen live on 2026-09-07:
          * a normal completion dict
          * HTTP 200 whose body is {"error":..., "id":...} with NO "choices" key -- 11% of
            probe calls, all on one provider. Status-code checking alone reads this as success.
          * a streaming AsyncIterator, where usage is not inspectable; counted as one message.
        """
        if not isinstance(result, dict):
            self.record_success(tier, model, latency_ms=latency_ms)
            return
        if "choices" not in result:
            self.record_empty_200(tier, model)
            return
        ch = (result.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        usage = result.get("usage") or {}
        det = usage.get("completion_tokens_details") or {}
        content = msg.get("content") or ""
        self.record_success(
            tier, model,
            tokens_in=usage.get("prompt_tokens") or 0,
            tokens_out=usage.get("completion_tokens") or 0,
            reasoning_tokens=det.get("reasoning_tokens") or 0,
            latency_ms=latency_ms,
            finish_reason=ch.get("finish_reason"),
            chars=len(content),
        )

    def record_exception(self, tier: str, model: str, exc: BaseException) -> None:
        """Classify a raised backend error. 429 gets the long cooldown; anything else is
        counted as an error but does NOT sideline the model -- a one-off is not a rate signal."""
        text = "%s %s" % (type(exc).__name__, exc)
        if "429" in text or "rate limit" in text.lower():
            self.record_429(tier, model)
            return
        with self._lock:
            self._roll_day()
            self._minute.append(time.time())
            self._tier(tier).errors += 1

    # -- observability -----------------------------------------------------------------
    def report(self) -> dict:
        with self._lock:
            self._roll_day()
            per_tier = {}
            for tier, c in sorted(self._tiers.items()):
                lat = sorted(c.latency_ms)
                per_tier[tier] = {
                    "messages": c.messages,
                    "tokens_in": c.tokens_in,
                    "tokens_out": c.tokens_out,
                    "reasoning_tokens": c.reasoning_tokens,
                    "http_429": c.http_429,
                    "empty_200": c.empty_200,
                    "errors": c.errors,
                    "capped": c.capped,
                    "starved": c.starved,
                    "median_ms": lat[len(lat) // 2] if lat else None,
                }
            return {
                "day": self._day_key,
                "shared_daily_used": self._used_today(),
                "shared_daily_cap": DAILY_CAP,
                "shared_daily_remaining": max(0, DAILY_CAP - self._used_today()),
                "per_minute_used": len(self._minute),
                "per_minute_cap": PER_MINUTE_CAP,
                "cooling_down": {m: round(t - time.time(), 1)
                                 for m, t in self._cooldown.items() if t > time.time()},
                "per_tier": per_tier,
                "caveat": "counts only calls this router made; a shared key under-reads",
            }


_SINGLETON: FreeTierBudget | None = None


def get_budget() -> FreeTierBudget:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = FreeTierBudget()
    return _SINGLETON


if __name__ == "__main__":
    import json
    b = get_budget()
    print("daily count read from telemetry:", b._today_from_db())
    print(json.dumps(b.report(), indent=2))
    print(b.check("3-synthesis", role="assembly"))
