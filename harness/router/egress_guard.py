"""Cloud-egress circuit breaker + reversible sanitizer (CIRCUIT-BREAKER-SANITIZE-CASCADE).

PROTECTED: protected_files.json rule 1 by path, and c2 via main.py's module-level import
(corrected 2026-10-12, CEREMONY 114 -- this line used to say "Non-protected module", which
stopped being true when it was listed). main.py calls into it at the local->cloud cascade boundary -- the
"4th physical endpoint", the ARCHITECTED sanctioned escalation tier (3-* escalation_only
tiers, e.g. 3-claude). This is NOT the separate Drive/Gemini dev-tooling egress boundary.

Two INDEPENDENT behaviors at one hook point:

  (1) CloudBreaker -- a per-process circuit breaker over the sanctioned cloud tier.
      Sustained cloud failures TRIP it; while tripped the caller drops the escalation
      (cloud) tiers from the cascade -- the SAME effect as BIFROST_AIR_GAPPED / the v117
      `_non_esc` split -- instead of paying backends/_retry.py's 3x exponential backoff on
      every request while cloud is down. After a cooldown a single half-open probe is
      allowed. COMPLEMENTS backends/_retry.py: retry = transient per-call blips (429/503/
      504); breaker = sustained outage fail-fast. It does NOT reimplement retry.

  (2) sanitize_outbound / reconstitute_inbound -- DETERMINISTIC, REVERSIBLE substitution
      of sensitive tokens (API keys/secrets, bearer tokens, absolute user paths, private
      IPs, emails) with opaque placeholders BEFORE a task crosses to cloud, and exact
      restoration of the cloud's answer on return. A reversible substitution GUARANTEES
      fidelity (no LLM round-trip corruption) and a PROVABLE "no raw sensitive token
      crossed" invariant (see assert_no_leak). The per-request mapping stays in the Router
      and is never sent. An LLM-based task-abstraction layer (local gemma3:4b / 1a-hearth
      re-phrasing the whole task) is a SEPARATE, gated enhancement -- deliberately NOT here,
      because an LLM abstraction/reconstitution round-trip is lossy and cannot offer the
      reversible module's fidelity or leak guarantee.
"""
from __future__ import annotations

import json
import re
import time


# ---------------------------------------------------------------------------
# (1) CloudBreaker
# ---------------------------------------------------------------------------
class CloudBreaker:
    """Three-state breaker: closed -> open (after `fail_threshold` consecutive
    cloud failures) -> half_open (after `cooldown_s`) -> closed on a successful
    probe, or back to open on a failed probe. `now` is injectable for tests;
    at runtime it defaults to time.monotonic()."""

    def __init__(self, fail_threshold: int = 3, cooldown_s: float = 30.0):
        self.fail_threshold = fail_threshold
        self.cooldown_s = cooldown_s
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open = False

    def _now(self, now: float | None) -> float:
        return now if now is not None else time.monotonic()

    def allow_cloud(self, now: float | None = None) -> bool:
        """True if a cloud call should be attempted now. When open, returns False
        until the cooldown elapses, then permits exactly one half-open probe."""
        if self._opened_at is None:
            return True  # closed
        if self._now(now) - self._opened_at >= self.cooldown_s:
            self._half_open = True  # allow one probe
            return True
        return False  # open, cooling down

    def record_success(self, now: float | None = None) -> None:
        self._consecutive_failures = 0
        self._opened_at = None
        self._half_open = False

    def record_failure(self, now: float | None = None) -> None:
        self._consecutive_failures += 1
        if self._half_open:
            # a probe failed -> re-open and restart the cooldown
            self._opened_at = self._now(now)
            self._half_open = False
        elif self._consecutive_failures >= self.fail_threshold and self._opened_at is None:
            self._opened_at = self._now(now)

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "half_open" if self._half_open else "open"


# Module-level singleton breaker over the sanctioned cloud tier. main.py imports this.
CLOUD_BREAKER = CloudBreaker()


def is_cloud_tier_value(tier_value: str) -> bool:
    """A sanctioned cloud/escalation tier is a `3-*` Tier enum value (3-claude, 3-gemini,
    3-openrouter, ...). String-based so this module has no import cycle with config.py."""
    return isinstance(tier_value, str) and tier_value.startswith("3-")


# ---------------------------------------------------------------------------
# (2) Reversible sanitizer
# ---------------------------------------------------------------------------
# Order matters: most specific / highest-entropy first so a broad pattern cannot
# swallow a token a narrower one should own. Each entry: (KIND, compiled-regex).
_PATTERNS: list[tuple[str, re.Pattern]] = [
    # provider secret formats (Anthropic sk-ant-*, OpenAI sk-, GitHub gh?_, AWS AKIA, Slack xox*).
    # 2026-07-05: sk-ant- added FIRST -- Anthropic keys have internal hyphens (sk-ant-api03-...) that
    # break the generic sk-[A-Za-z0-9]{16,} pattern, so they were leaking through un-masked (found by
    # the 2b live-pod sanitize smoke on the CC-facing Router, which itself uses ANTHROPIC_API_KEY).
    ("SECRET", re.compile(r"\b(?:sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b")),
    # (?!__BIFROST_) 2026-10-12, CEREMONY 114: "Bearer __BIFROST_SECRET_1__" must not be wrapped
    # again. That is what makes a second pass over masked text a no-op, so the gate no longer has
    # to skip any string that already holds a placeholder (see sanitize_messages_secrets).
    ("BEARER", re.compile(r"(?i)\bbearer\s+(?!__BIFROST_)[A-Za-z0-9._\-]{16,}")),
    ("EMAIL",  re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("WINPATH", re.compile(r"[A-Za-z]:\\Users\\[^\\/:*?\"<>|\r\n]+")),
    ("NIXPATH", re.compile(r"/(?:home|Users)/[^/\s\"':;,]+")),
    ("PRIVIP", re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")),
    # JSON Web Token, by SHAPE -- the rule lane-proven as u12 `jwtscan` (48 checks, 16 mutations):
    # exactly three dot-separated, non-empty base64url segments, the first opening "eyJ" (base64url
    # of '{"'). A ServiceAccount token is a JWT and carries no sk-/Bearer prefix, so it crossed to
    # cloud unmasked. The lookarounds keep a 4-segment run from matching its first three, while a
    # sentence-ending period still ends a real token. 2026-10-12, CEREMONY 114.
    ("JWT", re.compile(r"(?<![A-Za-z0-9_.-])eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
                       r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-])")),
    # Bare high-entropy hex. LAST because it is the LEAST specific -- the provider
    # formats above must claim their tokens first. 2026-10-06: ported verbatim from
    # bifrost_shell.py:311, which governs a DIFFERENT boundary (tool output to a LOCAL
    # model) and was never carried to this one, so a SHA-256-length key carrying no
    # sk-/xox/AKIA prefix and no key=value shape crossed to cloud unmasked.
    # Excluding EXACTLY 40 is load-bearing and is not an off-by-one: a 40-char git
    # SHA-1 is not a secret, and the earlier fix that spared commit hashes by raising
    # the floor to {65,} also stopped redacting every 40-64 char secret. Excluding 40
    # keeps hashes readable and redacts everything from 41 up.
    ("HEXSECRET", re.compile(r"\b(?![A-Fa-f0-9]{40}\b)[A-Fa-f0-9]{40,}\b")),
]

_PLACEHOLDER = re.compile(r"__BIFROST_[A-Z]+_\d+__")


# Leak-critical classes only. SECRET/BEARER/HEXSECRET are credentials that must NEVER
# egress; EMAIL/WINPATH/NIXPATH/PRIVIP are privacy/fidelity concerns whose masking would
# degrade the cloud's context without a leak-severity payoff -> excluded from the gate.
# HEXSECRET registered here 2026-10-06, and THIS LINE is what arms it: the outbound gate
# (sanitize_messages_secrets) filters on this set, so a pattern added to _PATTERNS alone
# is inert at the cloud boundary. Constructed is not registered.
# JWT registered 2026-10-12 (CEREMONY 114), for the same reason.
_SECRET_KINDS = frozenset({"SECRET", "BEARER", "HEXSECRET", "JWT"})


def sanitize_outbound(text: str, kinds: frozenset[str] | None = None) -> tuple[str, dict[str, str]]:
    """Replace sensitive tokens with opaque, reversible placeholders. Returns
    (sanitized_text, mapping) where mapping is {placeholder: original}. Identical
    originals reuse ONE placeholder so the cloud can still reason about repeated
    entities. The mapping never leaves the Router. `kinds` (default = all _PATTERNS)
    restricts which classes are masked -- e.g. _SECRET_KINDS for the credential-only gate."""
    if not text:
        return text, {}
    mapping: dict[str, str] = {}
    orig_to_ph: dict[str, str] = {}
    counters: dict[str, int] = {}

    def _make_repl(kind: str):
        def _r(m: re.Match) -> str:
            orig = m.group(0)
            if orig in orig_to_ph:
                return orig_to_ph[orig]
            counters[kind] = counters.get(kind, 0) + 1
            ph = f"__BIFROST_{kind}_{counters[kind]}__"
            mapping[ph] = orig
            orig_to_ph[orig] = ph
            return ph
        return _r

    out = text
    for kind, pat in _PATTERNS:
        if kinds is not None and kind not in kinds:
            continue
        out = pat.sub(_make_repl(kind), out)
    return out, mapping


def sanitize_messages_secrets(messages: list[dict]) -> tuple[list[dict], dict[str, str], int]:
    """Credential-only outbound gate for a chat-messages list. Walks each message's
    `content` (both plain-string and Anthropic list-of-blocks shapes) AND, since CEREMONY 114,
    each assistant `tool_calls[*].function.arguments`, masking ONLY the _SECRET_KINDS
    (SECRET/BEARER/HEXSECRET/JWT) so a leaked key can never cross to a cloud tier. Returns
    (new_messages, merged_mapping, num_masked). Non-mutating: builds a fresh list and
    only shallow-copies the messages that actually changed. No inbound reconstitution
    is paired -- the client never needs a masked credential echoed back, and the cloud
    model can transform a placeholder mid-stream (making round-trip reversal unreliable),
    so masking is one-way by design."""
    if not messages:
        return messages, {}, 0
    mapping: dict[str, str] = {}
    n = 0

    def _san(s: str) -> str:
        nonlocal n
        # 2026-10-12, CEREMONY 114: NO placeholder skip. `"__BIFROST_" in s` used to return the
        # string untouched, so a raw credential crossed whenever the same string also held an
        # echoed placeholder. A re-scan is idempotent by construction: no _SECRET_KINDS pattern
        # matches a placeholder (BEARER refuses one explicitly).
        if not s:
            return s
        out, m = sanitize_outbound(s, kinds=_SECRET_KINDS)
        if m:
            mapping.update(m)
            n += len(m)
        return out

    new_msgs: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            sanitized = _san(content)
            if sanitized is not content:
                msg = {**msg, "content": sanitized}
        elif isinstance(content, list):
            new_blocks = []
            changed = False
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    st = _san(block["text"])
                    if st is not block["text"]:
                        block = {**block, "text": st}
                        changed = True
                new_blocks.append(block)
            if changed:
                msg = {**msg, "content": new_blocks}
        # 2026-10-12, CEREMONY 114: an assistant's tool_calls carry their arguments OUTSIDE
        # `content` and were never walked, so a credential a local tier put into a tool call
        # crossed when the conversation escalated to cloud. The arguments are a JSON string;
        # masking inside it as text keeps it valid JSON (a placeholder is [A-Z0-9_]).
        calls = msg.get("tool_calls")
        if isinstance(calls, list):
            new_calls, calls_changed = [], False
            for call in calls:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                    sa = _san(fn["arguments"])
                    if sa is not fn["arguments"]:
                        call = {**call, "function": {**fn, "arguments": sa}}
                        calls_changed = True
                new_calls.append(call)
            if calls_changed:
                msg = {**msg, "tool_calls": new_calls}
        new_msgs.append(msg)
    return new_msgs, mapping, n


def reconstitute_inbound(text: str, mapping: dict[str, str]) -> str:
    """Restore original sensitive values in the cloud's response. Exact inverse of
    sanitize_outbound for placeholders the cloud echoed back verbatim."""
    if not text or not mapping:
        return text
    for ph, orig in mapping.items():
        text = text.replace(ph, orig)
    return text


def assert_no_leak(sanitized: str, mapping: dict[str, str]) -> list[str]:
    """Leak invariant: NO original sensitive value may survive in the outbound text.
    Returns the list of leaked originals (empty == clean). main.py calls it, through
    outbound_leak_report, at the dispatch_to_tier choke point (CEREMONY 114). Until
    2026-10-12 this docstring said main.py did so, and it had ZERO call sites."""
    if not sanitized or not mapping:
        return []
    return [orig for orig in mapping.values() if orig in sanitized]


_PH_KIND = re.compile(r"__BIFROST_([A-Z]+)_\d+__")


def outbound_leak_report(messages: list[dict], mapping: dict[str, str]) -> dict:
    """The leak invariant over the WHOLE outbound payload, as COUNTS ONLY -- it never returns a
    value, so the report itself can be logged. 2026-10-12, CEREMONY 114.

    The gate walks `content` and tool-call arguments; anything else in a message crosses
    unwalked. This serializes the entire list and answers two questions:
      leaked    how many originals the gate DID mask still appear elsewhere in the payload
                (assert_no_leak over the serialized list -- its first real caller);
      residual  per kind, how many distinct credential-shaped tokens remain anywhere,
                including ones the gate never saw because they sat only in an unwalked field.
    {"leaked": 0, "residual": {}} means clean."""
    if not messages:
        return {"leaked": 0, "residual": {}}
    try:
        payload = json.dumps(messages, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 -- a report must never break a dispatch
        payload = str(messages)
    leaked = len(assert_no_leak(payload, mapping))
    _, rmap = sanitize_outbound(payload, kinds=_SECRET_KINDS)
    residual: dict[str, int] = {}
    for ph in rmap:
        m = _PH_KIND.fullmatch(ph)
        kind = m.group(1) if m else "UNKNOWN"
        residual[kind] = residual.get(kind, 0) + 1
    return {"leaked": leaked, "residual": residual}
