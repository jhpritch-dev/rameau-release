"""bifrost_shell.delegate_subtask must refuse any leaf outside SAFE_LEAVES before a network call.

2026-09-11. The tool used to POST ANY leaf_alias to the Router (bifrost-auto, cloud tiers,
claude-* all dispatchable). The allowlist has one source: SAFE_LEAVES in bifrost_delegate.py,
which bifrost_shell reads with ast -- it must never import bifrost_delegate (that GETs
/v1/models and builds a second FastMCP server at import).

bifrost_shell.py is loaded by FILE PATH from this tree (never via a sys.path lookup that could
resolve the real repo). httpx.AsyncClient is replaced by a recorder and httpx.get by a
recorder that fails, so no test makes a live call and a POST or GET that should not happen
is visible. Matching is EXACT (no case folding, no strip), as in bifrost_delegate.delegate().
"""
import ast
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SHELL = _ROOT / "bifrost_shell.py"
_DELEGATE = _ROOT / "bifrost_delegate.py"


def _safe_leaf_names():
    """SAFE_LEAVES keys from bifrost_delegate.py, parsed here independently of bifrost_shell."""
    tree = ast.parse(_DELEGATE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "SAFE_LEAVES" for t in node.targets):
            return sorted(ast.literal_eval(node.value))
    raise AssertionError("SAFE_LEAVES not found in %s" % _DELEGATE)


SAFE = _safe_leaf_names()

# Router-advertised or Router-pinned names that are NOT in SAFE_LEAVES (cloud tiers, auto,
# claude-*, the Igor duplicate the old docstring advertised, M2a-removed leaves), plus junk.
NON_SAFE = [
    "bifrost-auto", "bifrost-cloud", "bifrost-gemini", "bifrost-groq", "bifrost-openrouter",
    "bifrost-perplexity", "bifrost-cerebras", "claude-opus-4-7", "bifrost-igor",
    "bifrost-t2-general", "bifrost-kat-long", "bifrost-npu", "bifrost-npu-tools",
    "bifrost-coder-tool", "n8n-ops", "", "not-a-leaf",
]
RESULT_KEYS = {"ok", "leaf", "response", "usage", "elapsed_s", "error"}


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
        self.gets = 0

    def factory(self):
        rec = self

        class _Client:
            def __init__(self, *a, **k):
                rec.clients += 1

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, json=None, **k):
                rec.posts.append({"url": url, "json": json})
                return _Resp()

        return _Client

    def get(self, *a, **k):
        self.gets += 1
        raise httpx.ConnectError("network disabled in test")


@pytest.fixture
def env(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(httpx, "AsyncClient", rec.factory())
    monkeypatch.setattr(httpx, "get", rec.get)
    monkeypatch.syspath_prepend(str(_ROOT))            # bifrost_shell imports checks.py
    spec = importlib.util.spec_from_file_location("bifrost_shell_under_test", _SHELL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert Path(mod.__file__).resolve() == _SHELL
    return mod, rec


def _call(mod, leaf, **kw):
    return json.loads(asyncio.run(mod.delegate_subtask(leaf, "ping", **kw)))


def test_allowlist_is_bifrost_delegate_safe_leaves(env):
    mod, _rec = env
    assert SAFE, "SAFE_LEAVES is empty"
    assert mod._SAFE_LEAVES_SRC.resolve() == _DELEGATE
    safe, why = mod._safe_leaves()
    assert why == "" and safe == frozenset(SAFE)


def test_allowlist_read_never_imports_bifrost_delegate(env):
    mod, rec = env
    before = set(sys.modules)
    mod._safe_leaves()
    _call(mod, "bifrost-auto")
    _call(mod, SAFE[0])
    assert not [m for m in set(sys.modules) - before if "bifrost_delegate" in m]
    assert rec.gets == 0                    # bifrost_delegate's import-time /v1/models GET


@pytest.mark.parametrize("leaf", NON_SAFE)
def test_non_safe_leaf_refused_before_any_http(env, leaf):
    mod, rec = env
    res = _call(mod, leaf)
    assert set(res) == RESULT_KEYS
    assert res["ok"] is False and "not in SAFE_LEAVES" in res["error"]
    assert res["leaf"] == leaf and res["response"] == "" and res["elapsed_s"] == 0.0
    assert rec.posts == [] and rec.clients == 0


@pytest.mark.parametrize("leaf", SAFE)
def test_each_safe_leaf_passes_guard(env, leaf):
    mod, rec = env
    res = _call(mod, leaf)
    assert res["ok"] is True, res["error"]
    assert res["response"] == "pong" and res["leaf"] == leaf
    assert rec.clients == 1 and len(rec.posts) == 1
    assert rec.posts[0]["json"]["model"] == leaf


@pytest.mark.parametrize("variant", [
    "BIFROST-GRANITE", "Bifrost-Granite", " bifrost-granite", "bifrost-granite ",
    "bifrost-granite\n", "\tbifrost-granite",
])
def test_case_and_whitespace_variants_refused_not_normalized(env, variant):
    mod, rec = env
    assert "bifrost-granite" in SAFE
    res = _call(mod, variant)
    assert res["ok"] is False and "not in SAFE_LEAVES" in res["error"]
    assert res["leaf"] == variant               # echoed verbatim, never folded
    assert rec.posts == [] and rec.clients == 0


@pytest.mark.parametrize("leaf", [None, 123, ["bifrost-granite"], {"leaf": "bifrost-granite"}])
def test_non_string_leaf_refused_and_never_raises(env, leaf):
    mod, rec = env
    res = _call(mod, leaf)
    assert res["ok"] is False and "SAFE_LEAVES" in res["error"]
    assert res["leaf"] == repr(leaf)
    assert rec.posts == [] and rec.clients == 0


@pytest.mark.parametrize("body", [
    None,                                            # file missing
    "X = 1\n",                                       # no SAFE_LEAVES
    "SAFE_LEAVES = {}\n",                            # empty
    "SAFE_LEAVES = dict(a=1)\n",                     # not a literal
    "SAFE_LEAVES = {\n",                             # syntax error
])
def test_allowlist_unavailable_fails_closed(env, monkeypatch, tmp_path, body):
    mod, rec = env
    src = tmp_path / "bifrost_delegate.py"
    if body is not None:
        src.write_text(body, encoding="utf-8")
    monkeypatch.setattr(mod, "_SAFE_LEAVES_SRC", src)
    safe, why = mod._safe_leaves()
    assert safe is None and why
    res = _call(mod, SAFE[0])                   # even a SAFE leaf is refused
    assert res["ok"] is False and "fail-closed" in res["error"]
    assert rec.posts == [] and rec.clients == 0


def test_policy_gate_still_runs_first(env, monkeypatch):
    mod, rec = env
    monkeypatch.setattr(mod, "_POLICY", {"classes": {"DELEGATE": {"enabled": False}}, "tools": {}})
    res = asyncio.run(mod.delegate_subtask(SAFE[0], "ping"))
    assert isinstance(res, dict) and res.get("policy_denied") is True
    assert rec.posts == [] and rec.clients == 0


def test_registered_mcp_tool_is_guarded(env):
    """Through FastMCP's own call path (the gateway's path), not just the Python function."""
    mod, rec = env
    from fastmcp import Client

    async def go():
        async with Client(mod.mcp) as c:
            bad = await c.call_tool("delegate_subtask",
                                    {"leaf_alias": "bifrost-auto", "subtask": "ping"},
                                    raise_on_error=False)
            good = await c.call_tool("delegate_subtask",
                                     {"leaf_alias": SAFE[0], "subtask": "ping"},
                                     raise_on_error=False)
            return json.loads(bad.content[0].text), json.loads(good.content[0].text)

    bad, good = asyncio.run(go())
    assert bad["ok"] is False and "not in SAFE_LEAVES" in bad["error"]
    assert good["ok"] is True, good["error"]
    assert len(rec.posts) == 1 and rec.posts[0]["json"]["model"] == SAFE[0]
