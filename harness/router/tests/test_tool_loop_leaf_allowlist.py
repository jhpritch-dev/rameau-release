"""igor_tool_loop delegate_subtask mode="tools" must refuse any leaf outside SAFE_LEAVES.

2026-09-11. The in-process leaf recursion resolved leaf_alias through tier_resolver (main.py's
_MODEL_ALIAS_TIER_PINS), which pins cloud tiers (bifrost-cerebras, bifrost-reasoning, ...) and
candidate/duplicate aliases. The only gates were the capability registry's tool_calls:false and
"no pinned tier". The allowlist has one source: SAFE_LEAVES in bifrost_delegate.py, read with ast
(never imported: that GETs /v1/models and builds a FastMCP server).

No test makes a live call: the backend and the gateway are recorders and httpx.get fails loudly.
The REAL pin table is parsed from this tree's main.py TEXT (main is never imported), and the REAL
capability registry is used wherever a test does not say otherwise.
"""
import ast
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import httpx
import pytest

import capability_registry
import igor_tool_loop as itl

ROOT = Path(__file__).resolve().parents[1]
_DELEGATE = ROOT / "bifrost_delegate.py"
_MAIN = ROOT / "main.py"
PARENT = "T_PARENT"


def _safe_leaf_names():
    """SAFE_LEAVES keys from bifrost_delegate.py, parsed here independently of igor_tool_loop."""
    tree = ast.parse(_DELEGATE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "SAFE_LEAVES" for t in node.targets):
            return sorted(ast.literal_eval(node.value))
    raise AssertionError("SAFE_LEAVES not found in %s" % _DELEGATE)


def _pin_table():
    """alias -> Tier member name, from main.py's _MODEL_ALIAS_TIER_PINS literal (text only)."""
    src = _MAIN.read_text(encoding="utf-8", errors="replace")
    i = src.index("_MODEL_ALIAS_TIER_PINS = {")
    j = src.index("\n}", i)
    pins = dict(re.findall(r'^\s*"([^"]+)":\s*Tier\.(\w+)', src[i:j], re.M))
    assert len(pins) > 20, pins
    return pins


def _registry_lets_through(alias):
    """True when the REAL registry does not stop mode=tools (absent, or tool_calls truthy)."""
    e = capability_registry.get_alias(alias)
    return e is None or bool(e.get("tool_calls"))


SAFE = _safe_leaf_names()
PINS = _pin_table()
# Real pinned aliases that are NOT safe, and that the real registry lets through today.
NON_SAFE_REACHABLE = sorted(a for a in PINS if a not in SAFE and _registry_lets_through(a))
# Every pinned non-SAFE alias plus unpinned names, for the registry-independent tests.
NON_SAFE_ANY = sorted(a for a in PINS if a not in SAFE) + [
    "bifrost-auto", "claude-opus-4-7", "", "not-a-leaf"]


def _leaf_tier(alias):
    return "LEAF_" + PINS.get(alias, "X_" + alias)


REAL_RESOLVER = {a: _leaf_tier(a) for a in PINS}.get


def _delegate(i, alias, mode="tools", **extra):
    a = {"leaf_alias": alias, "subtask": "leaf task", "mode": mode}
    a.update(extra)
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"d{i}", "function": {"name": "delegate_subtask", "arguments": a}}]},
        "finish_reason": "tool_calls"}]}


def _final(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


class Backend:
    """Each tier replies from its own script; a tier with no script (or past it) answers at once."""

    def __init__(self, scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls = []

    async def __call__(self, **kw):
        tier = kw["tier"]
        self.calls.append({"tier": tier, "messages": list(kw["messages"])})
        n = sum(1 for c in self.calls if c["tier"] == tier) - 1
        script = self.scripts.get(tier, [])
        return script[n] if n < len(script) else _final(f"answer from {tier}")

    def tiers(self):
        return [c["tier"] for c in self.calls]

    def tool_results(self, tier):
        """The tool messages the model on `tier` saw on its LAST call."""
        last = [c for c in self.calls if c["tier"] == tier][-1]
        return [json.loads(m["content"]) for m in last["messages"] if m.get("role") == "tool"]


class Gateway:
    def __init__(self):
        self.calls = []

    async def __call__(self, name, arguments, request_id):
        self.calls.append((name, dict(arguments)))
        return {"ok": True, "result": {"text": "gateway ok"}}


@pytest.fixture(autouse=True)
def gets(monkeypatch, tmp_path):
    assert Path(itl.__file__).resolve().parent == ROOT      # never the real repo's module
    monkeypatch.setattr(itl, "_RECOVERY_PAUSE_S", 0.0, raising=False)
    monkeypatch.setattr(itl, "_VIOLATION_LOG", str(tmp_path / "violations.jsonl"))
    monkeypatch.setattr(itl, "_CANARY_EVENTS_LOG", str(tmp_path / "canary.jsonl"))
    seen = []

    def _no_get(*a, **k):
        seen.append(a)
        raise httpx.ConnectError("network disabled in test")

    monkeypatch.setattr(httpx, "get", _no_get)
    return seen


@pytest.fixture
def all_tool_capable(monkeypatch):
    """Registry says tool_calls:true for EVERY name, so only the SAFE_LEAVES guard can refuse."""
    monkeypatch.setattr(capability_registry, "get_alias", lambda name: {"alias": name, "tool_calls": True})


def _run(backend, resolver, gateway=None):
    return asyncio.run(itl.run_tool_loop(
        backend_dispatch_fn=backend, tier=PARENT,
        messages=[{"role": "user", "content": "fan out"}], temperature=0.0, max_tokens=None,
        caller_thinking=False, gateway_call_fn=gateway or Gateway(), max_iterations=4,
        total_turn_timeout_s=60.0, request_id="t-leaf", tier_resolver=resolver))


def _one(backend):
    res = backend.tool_results(PARENT)
    assert len(res) == 1, res
    return res[0]


# ------------------------------------------------------------------ the allowlist source
def test_allowlist_is_bifrost_delegate_safe_leaves():
    assert SAFE, "SAFE_LEAVES is empty"
    assert os.path.samefile(itl._SAFE_LEAVES_SRC, _DELEGATE)
    safe, why = itl._safe_leaves()
    assert why == "" and safe == frozenset(SAFE)


def test_allowlist_read_never_imports_bifrost_delegate(gets, all_tool_capable):
    before = set(sys.modules)
    itl._safe_leaves()
    _run(Backend({PARENT: [_delegate(1, "bifrost-auto"), _delegate(2, SAFE[0])]}), REAL_RESOLVER)
    assert not [m for m in set(sys.modules) - before if "bifrost_delegate" in m]
    assert gets == []                       # bifrost_delegate's import-time /v1/models GET


def test_the_real_tables_leave_non_safe_aliases_reachable():
    """Guards the parametrization below against going silently empty."""
    assert NON_SAFE_REACHABLE, "no non-SAFE pinned alias gets past the real registry any more"


# ------------------------------------------------------------------ refusals
@pytest.mark.parametrize("leaf", NON_SAFE_REACHABLE)
def test_real_tables_non_safe_leaf_refused_before_nested_loop(leaf):
    """REAL pin table + REAL registry: these ran a nested tool loop before the fix."""
    b, gw = Backend({PARENT: [_delegate(1, leaf)]}), Gateway()
    _run(b, REAL_RESOLVER, gw)
    res = _one(b)
    assert res["kind"] == "leaf_not_safe" and "not in SAFE_LEAVES" in res["error"], res
    assert b.tiers() == [PARENT, PARENT]            # no leaf-tier (nested) model call
    assert gw.calls == []


@pytest.mark.parametrize("leaf", NON_SAFE_ANY)
def test_any_non_safe_leaf_refused_even_if_registry_allows(all_tool_capable, leaf):
    resolver = {a: _leaf_tier(a) for a in NON_SAFE_ANY}.get
    b, gw = Backend({PARENT: [_delegate(1, leaf)]}), Gateway()
    _run(b, resolver, gw)
    res = _one(b)
    assert res["kind"] == "leaf_not_safe", res
    assert repr(leaf) in res["error"]
    assert b.tiers() == [PARENT, PARENT] and gw.calls == []


@pytest.mark.parametrize("variant", [
    "BIFROST-GRANITE", "Bifrost-Granite", " bifrost-granite", "bifrost-granite ",
    "bifrost-granite\n", "\tbifrost-granite",
])
def test_case_and_whitespace_variants_refused_not_normalized(all_tool_capable, variant):
    assert "bifrost-granite" in SAFE
    resolver = {variant: "LEAF_VARIANT"}.get
    b = Backend({PARENT: [_delegate(1, variant)]})
    _run(b, resolver)
    res = _one(b)
    assert res["kind"] == "leaf_not_safe" and repr(variant) in res["error"], res
    assert "LEAF_VARIANT" not in b.tiers()


@pytest.mark.parametrize("leaf", [None, 123, ["bifrost-granite"], {"leaf": "bifrost-granite"}])
def test_non_string_leaf_refused_and_never_raises(leaf):
    b = Backend({PARENT: [_delegate(1, leaf)]})
    out = _run(b, REAL_RESOLVER)                    # before the fix a list/dict raised TypeError
    assert out["choices"][0]["message"]["content"]
    res = _one(b)
    # G2 Finding 9 (John 2026-09-11, option a): an explicit null leaf_alias is a MISSING argument, so the
    # tool loop's phase-1 argument check refuses it (kind bad_arguments) before the leaf allowlist is
    # consulted. Either way the call is refused, nothing raises, and no leaf-tier model call happens.
    assert res["kind"] == ("bad_arguments" if leaf is None else "leaf_not_safe"), res
    assert b.tiers() == [PARENT, PARENT]


@pytest.mark.parametrize("body", [
    None,                                            # file missing
    "X = 1\n",                                       # no SAFE_LEAVES
    "SAFE_LEAVES = {}\n",                            # empty
    "SAFE_LEAVES = dict(a=1)\n",                     # not a literal
    "SAFE_LEAVES = {\n",                             # syntax error
])
def test_allowlist_unavailable_fails_closed(monkeypatch, tmp_path, body):
    src = tmp_path / "bifrost_delegate.py"
    if body is not None:
        src.write_text(body, encoding="utf-8")
    monkeypatch.setattr(itl, "_SAFE_LEAVES_SRC", str(src))
    safe, why = itl._safe_leaves()
    assert safe is None and why
    b = Backend({PARENT: [_delegate(1, "bifrost-granite")]})   # a SAFE, tool-capable, pinned leaf
    _run(b, REAL_RESOLVER)
    res = _one(b)
    assert res["kind"] == "leaf_not_safe" and "fail-closed" in res["error"], res
    assert b.tiers() == [PARENT, PARENT]


def test_guard_runs_before_registry_and_resolver(monkeypatch):
    seen = {"registry": 0, "resolver": 0}

    def _reg(name):
        seen["registry"] += 1
        return {"alias": name, "tool_calls": True}

    def _res(name):
        seen["resolver"] += 1
        return "LEAF_CLOUD"

    monkeypatch.setattr(capability_registry, "get_alias", _reg)
    b = Backend({PARENT: [_delegate(1, "bifrost-cerebras")]})
    _run(b, _res)
    assert _one(b)["kind"] == "leaf_not_safe"
    assert seen == {"registry": 0, "resolver": 0}
    assert "LEAF_CLOUD" not in b.tiers()


# ------------------------------------------------------------------ what still passes
@pytest.mark.parametrize("leaf", SAFE)
def test_each_safe_leaf_keeps_todays_outcome(leaf):
    """REAL tables: a tool-capable SAFE leaf runs exactly one nested loop on its pinned tier; a
    SAFE leaf the registry marks tool_calls:false (bifrost-coder) keeps its existing refusal."""
    b, gw = Backend({PARENT: [_delegate(1, leaf)]}), Gateway()
    _run(b, REAL_RESOLVER, gw)
    res = _one(b)
    assert gw.calls == []
    if _registry_lets_through(leaf):
        assert "error" not in res, res
        assert res["answer"] == f"answer from {_leaf_tier(leaf)}"
        assert res["trace"]["leaf_alias"] == leaf and res["trace"]["depth"] == 1
        assert b.tiers() == [PARENT, _leaf_tier(leaf), PARENT]
    else:
        assert res["kind"] == "leaf_not_tool_capable", res
        assert b.tiers() == [PARENT, PARENT]


def test_registry_refusal_order_unchanged_for_safe_tool_false_leaf():
    assert "bifrost-coder" in SAFE and not _registry_lets_through("bifrost-coder")
    b = Backend({PARENT: [_delegate(1, "bifrost-coder")]})
    _run(b, REAL_RESOLVER)
    assert _one(b)["kind"] == "leaf_not_tool_capable"


def test_safe_leaf_without_a_pin_is_still_leaf_unresolved(all_tool_capable):
    b = Backend({PARENT: [_delegate(1, SAFE[0])]})
    _run(b, {}.get)
    assert _one(b)["kind"] == "leaf_unresolved"


def test_nested_leaf_is_guarded_too(all_tool_capable):
    """depth 1: a SAFE leaf's own mode=tools delegation to a cloud alias is refused inside it."""
    resolver = {"bifrost-granite": "LEAF_T1B", "bifrost-cerebras": "LEAF_T3_CEREBRAS"}.get
    b = Backend({PARENT: [_delegate(1, "bifrost-granite")],
                 "LEAF_T1B": [_delegate(2, "bifrost-cerebras")]})
    _run(b, resolver)
    assert "LEAF_T3_CEREBRAS" not in b.tiers()
    inner = b.tool_results("LEAF_T1B")
    assert len(inner) == 1 and inner[0]["kind"] == "leaf_not_safe", inner
    assert "error" not in _one(b)                    # the SAFE leaf itself still answered


def test_reasoning_mode_is_not_this_guard(all_tool_capable):
    """mode=reasoning goes to the gateway as before; bifrost_shell.delegate_subtask guards it."""
    b, gw = Backend({PARENT: [_delegate(1, "bifrost-cerebras", mode="reasoning")]}), Gateway()
    _run(b, REAL_RESOLVER, gw)
    assert [c[0] for c in gw.calls] == ["delegate_subtask"]
    assert b.tiers() == [PARENT, PARENT]
