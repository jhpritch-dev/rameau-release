"""igor_tool_loop must not forward its own `mode` selector to bifrost_shell.delegate_subtask.

2026-09-11. delegate_subtask's schema in igor_tool_loop advertises `mode` (reasoning|tools).
mode="tools" is served in-process; every other delegate_subtask call went to the MCP gateway with
the model's arguments UNCHANGED. bifrost_shell.delegate_subtask(leaf_alias, subtask, timeout) has
no `mode`, and FastMCP validates arguments against that signature: an unexpected keyword is an
isError result and the tool body never runs. So a reasoning call in which the model named its
mode explicitly never reached the leaf (telemetry to 2026-08-22: 14 of 193 reasoning permits).

The end-to-end tests drive the REAL bifrost_shell.py from this tree, loaded by file path, through
FastMCP's in-memory Client (the gateway's call path). httpx.AsyncClient is a recorder and
httpx.get fails loudly, so no test makes a live call.
"""
import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

import igor_tool_loop as itl

ROOT = Path(__file__).resolve().parents[1]
_SHELL = ROOT / "bifrost_shell.py"
PARENT = "T_PARENT"
LEAF = "bifrost-granite"            # in SAFE_LEAVES, so bifrost_shell's own allowlist passes it


class _Resp:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": "pong"}}], "usage": {}}


class _Recorder:
    def __init__(self):
        self.posts = []

    def factory(self):
        rec = self

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, json=None, **k):
                rec.posts.append({"url": url, "json": json})
                return _Resp()

        return _Client


def _no_get(*a, **k):
    raise httpx.ConnectError("network disabled in test")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    assert Path(itl.__file__).resolve().parent == ROOT      # never the real repo's module
    monkeypatch.setattr(itl, "_RECOVERY_PAUSE_S", 0.0, raising=False)
    monkeypatch.setattr(itl, "_VIOLATION_LOG", str(tmp_path / "violations.jsonl"))
    monkeypatch.setattr(itl, "_CANARY_EVENTS_LOG", str(tmp_path / "canary.jsonl"))
    monkeypatch.setattr(httpx, "get", _no_get)
    return tmp_path


@pytest.fixture
def shell(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(httpx, "AsyncClient", rec.factory())
    monkeypatch.syspath_prepend(str(ROOT))                  # bifrost_shell imports checks.py
    spec = importlib.util.spec_from_file_location("bifrost_shell_mode_arg_under_test", _SHELL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert Path(mod.__file__).resolve() == _SHELL
    return mod, rec


def _mcp_gateway(mod):
    """A gateway_call_fn that goes through FastMCP's own call path, shaped like
    make_gateway_caller's result: isError -> ok False tool_error; else the parsed JSON."""
    from fastmcp import Client
    calls = []

    async def call(name, arguments, request_id):
        calls.append((name, dict(arguments)))
        async with Client(mod.mcp) as c:
            r = await c.call_tool(name, arguments, raise_on_error=False)
        text = "\n".join(getattr(x, "text", str(x)) for x in r.content)
        if r.is_error:
            return {"ok": False, "kind": "tool_error", "error": text[:500]}
        try:
            return {"ok": True, "result": json.loads(text)}
        except ValueError:
            return {"ok": True, "result": {"text": text}}

    call.calls = calls
    return call


class RecordingGateway:
    def __init__(self):
        self.calls = []

    async def __call__(self, name, arguments, request_id):
        self.calls.append((name, arguments))
        return {"ok": True, "result": {"text": "gateway ok"}}


def _tool_call(name, arguments):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "function": {"name": name, "arguments": arguments}}]},
        "finish_reason": "tool_calls"}]}


class Backend:
    def __init__(self, first):
        self.first = first
        self.calls = []

    async def __call__(self, **kw):
        self.calls.append({"tier": kw["tier"], "messages": list(kw["messages"])})
        if len(self.calls) == 1:
            return self.first
        return {"choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}]}

    def tool_result(self):
        msgs = [m for m in self.calls[1]["messages"] if m.get("role") == "tool"]
        assert len(msgs) == 1, msgs
        return json.loads(msgs[0]["content"])


def _run(backend, gateway, resolver=None):
    return asyncio.run(itl.run_tool_loop(
        backend_dispatch_fn=backend, tier=PARENT,
        messages=[{"role": "user", "content": "delegate it"}], temperature=0.0, max_tokens=None,
        caller_thinking=False, gateway_call_fn=gateway, max_iterations=4,
        total_turn_timeout_s=60.0, request_id="t-mode", tier_resolver=resolver))


# ------------------------------------------------------------------ the rejection itself
def test_served_schema_has_no_mode_and_fastmcp_rejects_it(shell):
    """Characterization (true before AND after this fix): the shell tool does not take `mode`,
    and FastMCP refuses the call without running the body."""
    mod, rec = shell
    from fastmcp import Client

    async def go():
        async with Client(mod.mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
            props = set((getattr(tools["delegate_subtask"], "inputSchema", None) or {}).get("properties", {}))
            r = await c.call_tool("delegate_subtask",
                                  {"leaf_alias": LEAF, "subtask": "ping", "mode": "reasoning"},
                                  raise_on_error=False)
            return props, r.is_error, "\n".join(getattr(x, "text", "") for x in r.content)

    props, is_error, text = asyncio.run(go())
    assert props == {"leaf_alias", "subtask", "timeout"}
    assert is_error is True and "mode" in text and "nexpected keyword" in text, text
    assert rec.posts == []


# ------------------------------------------------------------------ end to end, through FastMCP
@pytest.mark.parametrize("extra", [
    {"mode": "reasoning"},
    {"mode": "REASONING"},
    {"mode": "reasoning", "timeout": 60},
    {},                                               # no mode: worked before, still works
])
def test_reasoning_call_reaches_the_shell_tool(shell, extra):
    mod, rec = shell
    gw = _mcp_gateway(mod)
    args = {"leaf_alias": LEAF, "subtask": "ping", **extra}
    b = Backend(_tool_call("delegate_subtask", args))
    _run(b, gw)
    res = b.tool_result()
    assert "error" not in res or res.get("ok") is True, res
    assert res["ok"] is True and res["leaf"] == LEAF and res["response"] == "pong", res
    assert len(rec.posts) == 1 and rec.posts[0]["json"]["model"] == LEAF
    assert "mode" not in gw.calls[0][1]


# ------------------------------------------------------------------ the forwarded arguments
def test_gateway_args_drop_only_mode():
    gw = RecordingGateway()
    args = {"leaf_alias": LEAF, "subtask": "ping", "timeout": 90, "mode": "reasoning"}
    _run(Backend(_tool_call("delegate_subtask", dict(args))), gw)
    assert len(gw.calls) == 1
    name, sent = gw.calls[0]
    assert name == "delegate_subtask"
    assert sent == {"leaf_alias": LEAF, "subtask": "ping", "timeout": 90}


def test_unknown_mode_value_is_routed_as_reasoning_without_the_key():
    """Anything but "tools" was already routed to the gateway; it now arrives without `mode`."""
    gw = RecordingGateway()
    _run(Backend(_tool_call("delegate_subtask", {"leaf_alias": LEAF, "subtask": "x", "mode": "toolz"})), gw)
    assert gw.calls == [("delegate_subtask", {"leaf_alias": LEAF, "subtask": "x"})]


def test_other_tools_keep_their_own_mode_argument():
    assert "git_read" in itl.SAFE_TOOL_NAMES
    gw = RecordingGateway()
    args = {"mode": "show", "ref": "HEAD"}
    _run(Backend(_tool_call("git_read", dict(args))), gw)
    assert gw.calls == [("git_read", args)]


def test_telemetry_still_records_the_mode_key(_isolated):
    gw = RecordingGateway()
    _run(Backend(_tool_call("delegate_subtask", {"leaf_alias": LEAF, "subtask": "x", "mode": "reasoning"})), gw)
    recs = [json.loads(line) for line in (_isolated / "violations.jsonl").read_text(encoding="utf-8").splitlines()]
    permits = [r for r in recs if r["decision"] == "DELEGATE_PERMITTED"]
    assert len(permits) == 1 and permits[0]["mode"] == "reasoning"
    assert set(permits[0]["args_shape"]) == {"leaf_alias", "subtask", "mode"}


def test_mode_tools_still_never_reaches_the_gateway():
    gw = RecordingGateway()
    b = Backend(_tool_call("delegate_subtask", {"leaf_alias": LEAF, "subtask": "x", "mode": "tools"}))
    _run(b, gw, resolver={LEAF: "LEAF_T1B"}.get)
    assert gw.calls == []
    assert [c["tier"] for c in b.calls] == [PARENT, "LEAF_T1B", PARENT]
