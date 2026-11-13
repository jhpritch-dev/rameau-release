"""CEREMONY 122: the credential gate on the two cloud paths that never pass main.dispatch_to_tier.

TWO_PASS's cloud review (strategies.two_pass_stream) and AUTOPILOT's free T3 legs (subtask_graph._call_tier)
called their cloud backends directly, so egress_guard.sanitize_messages_secrets -- called only in
main.dispatch_to_tier -- never saw those messages. Backends are stubbed; nothing leaves the process.
The *_masks_* cases FAIL on b65fed8 (the 2026-10-13 audit's pre-tree run); the local cases pass on both.
"""

import asyncio

import config

# Built at runtime: the pre-push secret gate is line-based. Matches egress_guard SECRET (sk-[A-Za-z0-9]{16,}).
FAKE_KEY = "sk-" + "A1b2C3d4" * 4


def _sse(text):
    async def gen():
        if text:
            yield 'data: {"choices":[{"delta":{"content":"%s"}}]}\n\n' % text
        yield "data: [DONE]\n\n"
    return gen()


def _two_pass(strategies, msgs):
    async def run():
        async for _ in strategies.two_pass_stream(messages=msgs, band=config.ComplexityBand.COMPLEX,
                                                   local_tier=config.Tier.T1B, temperature=0.2, max_tokens=64):
            pass
    asyncio.run(run())


def test_two_pass_cloud_review_masks_a_credential(monkeypatch):
    import strategies
    captured = {}

    async def fake_draft(**kw):
        return _sse("")            # empty draft -> the original messages go to the review

    async def fake_cloud(**kw):
        captured["messages"] = kw["messages"]
        return _sse("ok")

    monkeypatch.setattr(strategies, "ollama_chat_completion", fake_draft)
    monkeypatch.setattr(strategies, "openai_compat_chat_completion", fake_cloud)
    monkeypatch.setattr(strategies, "anthropic_chat_completion", fake_cloud)
    # the live config reviews LOCALLY (local_review_preferred=True); force the cloud branch the gate guards
    monkeypatch.setattr(strategies, "get_review_target", lambda band, mode=None: ("cloud", "openrouter", "x/y:free"))
    _two_pass(strategies, [{"role": "user", "content": "deploy it with key " + FAKE_KEY + " please"}])
    assert "messages" in captured, "the cloud review was never called"
    assert FAKE_KEY not in str(captured["messages"]), "the TWO_PASS cloud review sent the raw credential"


def test_two_pass_local_review_is_unchanged(monkeypatch):
    import strategies
    captured = {}

    async def fake_ollama(**kw):
        if kw.get("tier") == config.Tier.T1B:
            return _sse("")                      # the draft
        captured["tier"] = kw.get("tier")
        captured["messages"] = kw["messages"]    # the local review
        return _sse("ok")

    monkeypatch.setattr(strategies, "ollama_chat_completion", fake_ollama)
    monkeypatch.setattr(strategies, "get_review_target", lambda band, mode=None: ("local", "2.5-solo", None))
    _two_pass(strategies, [{"role": "user", "content": "key " + FAKE_KEY}])
    assert captured.get("tier") == config.Tier.T2_5_SOLO
    assert FAKE_KEY in str(captured["messages"]), "a LOCAL review must see the original text"


def test_autopilot_free_leg_masks_a_credential(monkeypatch):
    import subtask_graph
    import backends.openai_compat as oc
    import free_tier_budget
    captured = {}

    async def fake_oc(**kw):
        captured["messages"] = kw["messages"]
        return {"choices": [{"message": {"content": "done"}}], "usage": {"total_tokens": 2}}

    monkeypatch.setattr(oc, "openai_compat_chat_completion", fake_oc)
    monkeypatch.setattr(free_tier_budget, "get_budget", lambda: None)
    msgs = [{"role": "user", "content": "use " + FAKE_KEY + " to call the API"}]
    text, _raw, _tok, _cost, _usage = asyncio.run(subtask_graph._call_tier("3-synthesis", msgs))
    assert "messages" in captured, "the free leg never reached openai_compat"
    assert text == "done"
    assert FAKE_KEY not in str(captured["messages"]), "an AUTOPILOT free T3 leg sent the raw credential"
    assert FAKE_KEY in msgs[0]["content"], "the gate must not mutate the caller's list"


def test_autopilot_local_leg_is_unchanged(monkeypatch):
    import subtask_graph
    captured = {}

    async def fake_ollama(**kw):
        captured["messages"] = kw["messages"]
        return {"choices": [{"message": {"content": "local"}}], "usage": {}}

    monkeypatch.setattr(subtask_graph, "ollama_chat_completion", fake_ollama)
    asyncio.run(subtask_graph._call_tier("1b", [{"role": "user", "content": "key " + FAKE_KEY}]))
    assert FAKE_KEY in str(captured["messages"]), "a LOCAL leg must see the original text"
