"""delegate() must refuse any leaf outside SAFE_LEAVES before a network call (2026-09-11).

bifrost_delegate.py fetches /v1/models AT IMPORT, so it is loaded here by FILE PATH from this
tree (never via sys.path, which could resolve the real repo) with httpx.get patched to fail.
The chat transport (httpx.AsyncClient) is replaced by a recorder, so no test makes a live call;
a POST that should not happen is visible as a recorded request.

Leaf matching in bifrost_delegate.py is EXACT everywhere (_route, _REGISTRY.get, the
_LIVE_ALIASES membership test): no case folding, no strip. The guard must match that, so a
case or whitespace variant of a SAFE leaf is refused, not normalized.
"""
import ast
import asyncio
import importlib.util
from pathlib import Path

import httpx
import pytest

_SRC = Path(__file__).resolve().parents[1] / "bifrost_delegate.py"


def _safe_leaf_names():
    """SAFE_LEAVES keys read from the source with ast -- no import at collection time."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "SAFE_LEAVES" for t in node.targets):
            return sorted(ast.literal_eval(node.value))
    raise AssertionError("SAFE_LEAVES not found in %s" % _SRC)


SAFE = _safe_leaf_names()

# Names the live Router advertised on 2026-09-11 that are NOT in SAFE_LEAVES, plus the two
# leaves M2a removed and a docstring-advertised alias that was never in the table.
NON_SAFE = [
    "bifrost-auto", "bifrost-cloud", "bifrost-gemini", "bifrost-groq", "bifrost-openrouter",
    "claude-opus-4-7", "bifrost-igor", "bifrost-npu-tools", "bifrost-coder-tool", "n8n-ops",
    "", "not-a-leaf",
]


class _Resp:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": "pong"}}], "usage": {}}


class _Recorder:
    def __init__(self):
        self.posts = []
        self.clients = 0

    def factory(self):
        rec = self

        class _Client:
            def __init__(self, *a, **k):
                rec.clients += 1

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, json=None, headers=None, **k):
                rec.posts.append({"url": url, "json": json, "headers": headers})
                return _Resp()

        return _Client


@pytest.fixture
def env(monkeypatch):
    def _no_net(*a, **k):
        raise httpx.ConnectError("network disabled in test")

    monkeypatch.setattr(httpx, "get", _no_net)
    spec = importlib.util.spec_from_file_location("bifrost_delegate_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rec = _Recorder()
    monkeypatch.setattr(httpx, "AsyncClient", rec.factory())
    monkeypatch.setattr(mod.httpx, "AsyncClient", rec.factory())
    # A known registry with no tool_calls:false rows, so the tools=True branch is reachable.
    monkeypatch.setattr(mod, "_REGISTRY", {})
    fn = getattr(mod.delegate, "fn", mod.delegate)   # FastMCP may wrap the function
    return mod, fn, rec


def _call(fn, leaf, **kw):
    return asyncio.run(fn(leaf, "ping", **kw))


def test_safe_table_parsed_matches_module(env):
    mod, _fn, _rec = env
    assert SAFE == sorted(mod.SAFE_LEAVES)
    assert SAFE, "SAFE_LEAVES is empty"


@pytest.mark.parametrize("live", ["unknown", "advertised"])
@pytest.mark.parametrize("leaf", NON_SAFE)
def test_non_safe_leaf_refused_before_any_http(env, monkeypatch, leaf, live):
    mod, fn, rec = env
    # "unknown": the /v1/models fetch failed (empty set) -- the live-alias check fails OPEN.
    # "advertised": the Router advertises the leaf -- the live-alias check would pass it.
    live_set = set() if live == "unknown" else set(SAFE) | {leaf}
    monkeypatch.setattr(mod, "_LIVE_ALIASES", live_set)
    res = _call(fn, leaf)
    assert res["ok"] is False
    assert "SAFE_LEAVES" in res["error"]
    assert res["leaf"] == leaf and res["content"] == "" and res["elapsed_s"] == 0.0
    assert set(res) == {"ok", "leaf", "content", "elapsed_s", "usage", "registry", "error"}
    assert rec.posts == [] and rec.clients == 0


@pytest.mark.parametrize("leaf", ["bifrost-auto", "bifrost-igor", "not-a-leaf"])
def test_non_safe_leaf_refused_with_tools_true(env, monkeypatch, leaf):
    mod, fn, rec = env
    monkeypatch.setattr(mod, "_LIVE_ALIASES", set(SAFE) | {leaf})
    res = _call(fn, leaf, tools=True)
    assert res["ok"] is False and "SAFE_LEAVES" in res["error"]
    assert rec.posts == [] and rec.clients == 0


@pytest.mark.parametrize("leaf", SAFE)
def test_each_safe_leaf_passes_guard(env, monkeypatch, leaf):
    mod, fn, rec = env
    monkeypatch.setattr(mod, "_LIVE_ALIASES", set(SAFE))
    res = _call(fn, leaf)
    assert res["ok"] is True, res["error"]
    assert res["content"] == "pong"
    assert len(rec.posts) == 1
    assert rec.posts[0]["json"]["model"] == mod._route(leaf)[0]


@pytest.mark.parametrize("leaf", SAFE)
def test_each_safe_leaf_passes_guard_when_live_list_unknown(env, monkeypatch, leaf):
    mod, fn, rec = env
    monkeypatch.setattr(mod, "_LIVE_ALIASES", set())
    res = _call(fn, leaf)
    assert res["ok"] is True, res["error"]
    assert len(rec.posts) == 1


@pytest.mark.parametrize("leaf", SAFE)
def test_each_safe_leaf_passes_guard_with_tools_true(env, monkeypatch, leaf):
    mod, fn, rec = env
    monkeypatch.setattr(mod, "_LIVE_ALIASES", set(SAFE))
    res = _call(fn, leaf, tools=True)
    assert res["ok"] is True, res["error"]
    assert res["tools_called"] == []
    assert len(rec.posts) == 1


@pytest.mark.parametrize("variant", [
    "BIFROST-GRANITE", "Bifrost-Granite", " bifrost-granite", "bifrost-granite ",
    "bifrost-granite\n", "\tbifrost-granite",
])
def test_case_and_whitespace_variants_are_refused_not_normalized(env, monkeypatch, variant):
    mod, fn, rec = env
    assert "bifrost-granite" in mod.SAFE_LEAVES
    monkeypatch.setattr(mod, "_LIVE_ALIASES", set())
    res = _call(fn, variant)
    assert res["ok"] is False and "SAFE_LEAVES" in res["error"]
    assert res["leaf"] == variant            # echoed verbatim, never folded
    assert rec.posts == [] and rec.clients == 0


@pytest.mark.parametrize("leaf", [None, 123, ["bifrost-granite"], {"leaf": "bifrost-granite"}])
def test_non_string_leaf_refused_and_never_raises(env, monkeypatch, leaf):
    mod, fn, rec = env
    monkeypatch.setattr(mod, "_LIVE_ALIASES", set())
    res = _call(fn, leaf)
    assert res["ok"] is False and "SAFE_LEAVES" in res["error"]
    assert res["registry"] is None
    assert rec.posts == [] and rec.clients == 0
