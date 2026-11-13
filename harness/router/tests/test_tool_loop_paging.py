"""Ceremony 4 Pattern 3: paged tool output, a cap summary, and a doom-loop hash.

  * A tool result over the per-result cap used to be cut, and the rest was gone. Now it is kept
    for the request and the model can page through it with tool_output_page (in-process; it never
    reaches the gateway; advertised only once a page exists).
  * At the iteration cap the loop used to return a synthetic apology. With real tool results in
    hand it now asks the model once, without tools, for a handoff (reason "cap_summary").
  * A call identical to the previous two is refused instead of executed.
"""
import asyncio
import itertools
import json
import re

import igor_tool_loop as itl

_SEQ = itertools.count()


def _tool_name():
    names = sorted(n for n in itl.SAFE_TOOL_NAMES if n not in ("delegate_subtask", itl.PAGE_TOOL_NAME))
    return "run_checks" if "run_checks" in names else names[0]


def _names(tools):
    return [d["function"]["name"] for d in (tools or [])]


class Backend:
    """Each entry is a response dict, or a callable(messages) -> response dict."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, **kw):
        self.calls.append({"messages": list(kw["messages"]), "tools": kw.get("tools")})
        i = len(self.calls) - 1
        r = self.responses[i] if i < len(self.responses) else _final("default final")
        return r(kw["messages"]) if callable(r) else r


class Gateway:
    def __init__(self, text="ok", ok=True):
        self.text, self.ok, self.calls = text, ok, []

    async def __call__(self, name, arguments, request_id):
        self.calls.append((name, arguments))
        if not self.ok:
            return {"ok": False, "error": "down", "kind": "transport"}
        return {"ok": True, "result": {"text": self.text}}


def _call(name, args, cid="c0"):
    return {"choices": [{"message": {"role": "assistant", "content": None,
                                     "tool_calls": [{"id": cid, "function": {"name": name, "arguments": args}}]},
                         "finish_reason": "tool_calls"}]}


def _tools_reply(args=None):
    return _call(_tool_name(), args if args is not None else {"seq": next(_SEQ)})


def _final(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def _run(backend, gateway, max_iterations=12):
    return asyncio.run(itl.run_tool_loop(
        backend_dispatch_fn=backend, tier="no-such-tier",      # unknown window -> cap 32 KB
        messages=[{"role": "user", "content": "check the repo"}],
        temperature=0.0, max_tokens=None, caller_thinking=False,
        gateway_call_fn=gateway, max_iterations=max_iterations, total_turn_timeout_s=60.0,
        request_id="t-page"))


def _tool_msgs(messages):
    return [m for m in messages if m.get("role") == "tool"]


# ---------------------------------------------------------------- policy + advertising
def test_policy_classifies_the_page_tool_as_read():
    assert itl._TOOL_CLASS_MAP.get(itl.PAGE_TOOL_NAME) == "READ"
    assert itl.PAGE_TOOL_NAME in itl.SAFE_TOOL_NAMES
    assert itl.PAGE_TOOL_NAME in itl._EXECUTABLE_ALLOWED, "else the breach canary would fire on it"
    assert itl.PAGE_TOOL_NAME not in _names(itl.get_safe_tool_definitions()), "never in the default list"


def test_a_loop_that_never_pages_sends_the_same_tools():
    be = Backend([_tools_reply(), _tools_reply(), _final("done")])
    _run(be, Gateway("short"))
    base = _names(itl.get_safe_tool_definitions())
    assert all(_names(c["tools"]) == base for c in be.calls)


# ---------------------------------------------------------------- paging
BIG = "".join(f"line {i:06d}\n" for i in range(5000))   # 60,000 chars, every slice distinct


def _page_from_hint(messages):
    last = _tool_msgs(messages)[-1]["content"]
    rid = re.search(r"tool_output_page\(id='([^']+)'", last).group(1)
    off = int(re.search(r"continue at offset=(\d+)", last).group(1))
    return _call(itl.PAGE_TOOL_NAME, {"id": rid, "offset": off, "limit": 4000}, cid="p1")


def test_an_oversize_result_is_paged_and_readable():
    be = Backend([_tools_reply(), _page_from_hint, _final("done")])
    gw = Gateway(BIG)
    out = _run(be, gw)
    stored = json.dumps({"text": BIG})
    first = _tool_msgs(be.calls[1]["messages"])[0]["content"]
    assert len(first) <= 32 * 1024 and first.startswith("[tool result truncated: ")
    assert stored[:2000] in first and first.endswith(stored[-2000:]), "head and tail kept verbatim"
    assert itl.PAGE_TOOL_NAME not in _names(be.calls[0]["tools"])
    assert _names(be.calls[1]["tools"])[-1] == itl.PAGE_TOOL_NAME, "advertised last, once a page exists"
    page = json.loads(_tool_msgs(be.calls[2]["messages"])[-1]["content"])
    off = int(re.search(r"continue at offset=(\d+)", first).group(1))
    assert page["id"] == "r0_0" and page["offset"] == off and page["returned"] == 4000
    assert page["text"] == stored[off:off + 4000] and page["total"] == len(stored)
    assert page["next_offset"] == off + 4000
    assert len(gw.calls) == 1, "tool_output_page never reaches the gateway"
    tl = out["_bifrost_tool_loop"]
    assert tl["reason"] == "completed" and tl["pages"] == 1 and tl["page_reads"] == 1
    assert tl["tools_called"] == [_tool_name(), itl.PAGE_TOOL_NAME]


def test_unknown_page_id_is_an_error_not_a_gateway_call():
    be = Backend([_call(itl.PAGE_TOOL_NAME, {"id": "r9_9"}), _final("done")])
    gw = Gateway()
    out = _run(be, gw)
    body = json.loads(_tool_msgs(be.calls[1]["messages"])[0]["content"])
    assert body["kind"] == "page_not_found" and gw.calls == []
    assert out["_bifrost_tool_loop"]["page_reads"] == 1


def test_page_reads_are_capped_below_the_result_cap():
    store = {"r0_0": "y" * 100000}
    r = itl._page_read(store, {"id": "r0_0", "offset": 10, "limit": 10 ** 9}, 32 * 1024)
    assert r["ok"] and r["result"]["returned"] == 16 * 1024 and r["result"]["offset"] == 10
    r = itl._page_read(store, {"id": "r0_0", "offset": "bad"}, 32 * 1024)
    assert r["ok"] and r["result"]["offset"] == 0


def test_store_evicts_oldest_beyond_its_bound(monkeypatch):
    monkeypatch.setattr(itl, "_PAGE_STORE_MAX_CHARS", 100)
    st = {}
    for k in range(4):
        itl._page_store_put(st, f"r{k}", "z" * 40)
    assert list(st) == ["r2", "r3"]


def test_without_the_policy_grant_it_falls_back_to_the_plain_cut(monkeypatch):
    real = itl._safe_for_role
    monkeypatch.setattr(itl, "_safe_for_role", lambda role: frozenset(real(role) - {itl.PAGE_TOOL_NAME}))
    be = Backend([_tools_reply(), _final("done")])
    _run(be, Gateway(BIG))
    first = _tool_msgs(be.calls[1]["messages"])[0]["content"]
    assert "[truncated " in first and itl.PAGE_TOOL_NAME not in first
    assert all(itl.PAGE_TOOL_NAME not in _names(c["tools"]) for c in be.calls)


# ---------------------------------------------------------------- cap summary
def test_cap_summary_replaces_the_apology():
    handoff = "Found: a.py:3 sets X=1. Unknown: who reads X. Next: grep_repo for X."
    be = Backend([_tools_reply() for _ in range(3)] + [_final(handoff)])
    out = _run(be, Gateway(), max_iterations=3)
    last = be.calls[3]
    assert last["tools"] is None and last["messages"][-1]["content"] == itl._CAP_SUMMARY_INSTRUCTION
    assert out["_bifrost_tool_loop"]["reason"] == "cap_summary"
    assert out["choices"][0]["message"]["content"] == handoff
    assert out["choices"][0]["finish_reason"] == "stop", "escalation-safe, like every terminal"


def test_an_empty_cap_summary_keeps_the_old_terminal():
    be = Backend([_tools_reply() for _ in range(3)] + [_final("   ")])
    out = _run(be, Gateway(), max_iterations=3)
    assert out["_bifrost_tool_loop"]["reason"] == "iteration_cap"
    assert "iteration limit" in out["choices"][0]["message"]["content"]


def test_a_cap_summary_that_calls_tools_is_ignored():
    be = Backend([_tools_reply() for _ in range(4)])
    gw = Gateway()
    out = _run(be, gw, max_iterations=3)
    assert len(be.calls) == 4 and len(gw.calls) == 3, "the summary reply's tool call is never run"
    assert out["_bifrost_tool_loop"]["reason"] == "iteration_cap"


def test_no_cap_summary_without_a_successful_tool_call():
    be = Backend([_tools_reply() for _ in range(3)])
    out = _run(be, Gateway(ok=False), max_iterations=3)
    assert len(be.calls) == 3
    assert out["_bifrost_tool_loop"]["reason"] == "no_successful_tool_exec"


# ---------------------------------------------------------------- doom loop
def test_third_identical_call_is_refused():
    same = {"q": "a"}
    be = Backend([_tools_reply(same), _tools_reply(same), _tools_reply(same), _final("done")])
    gw = Gateway()
    out = _run(be, gw)
    assert len(gw.calls) == 2
    body = json.loads(_tool_msgs(be.calls[3]["messages"])[-1]["content"])
    assert body["kind"] == "doom_loop" and "NOT executed" in body["error"]
    assert out["_bifrost_tool_loop"]["doom_refusals"] == 1


def test_key_order_does_not_hide_a_repeat():
    be = Backend([_tools_reply({"a": 1, "b": 2}), _tools_reply({"b": 2, "a": 1}),
                  _tools_reply({"a": 1, "b": 2}), _final("done")])
    gw = Gateway()
    _run(be, gw)
    assert len(gw.calls) == 2


def test_changed_arguments_are_not_a_doom_loop():
    be = Backend([_tools_reply({"q": "a"}), _tools_reply({"q": "a"}), _tools_reply({"q": "b"}), _final("done")])
    gw = Gateway()
    out = _run(be, gw)
    assert len(gw.calls) == 3 and out["_bifrost_tool_loop"]["doom_refusals"] == 0
