r"""Inline <think> leakage on the dispatch path.

The measured defect these cover, both from qwen3:1.7b on the Forge NPU (:8003):

  1. content began '<think>\nOkay, the user just said "Say OK."...' -- reasoning delivered to
     the caller verbatim, in the content field, unstripped.
  2. an unclosed <think> produced 1483 chars of reasoning with no answer,
     completion_tokens == max_tokens, and finish_reason "stop". The truncation was invisible.

`strip_think_blocks` exists (backends/ollama.py:336) but is reachable only from the DECOMPOSER
path, where its own docstring says stripping is wrong. So neither case was defended against.
"""

import pytest

from reasoning_coalesce import coalesce_reasoning_into_content


def resp(content, finish_reason="stop", **msg_extra):
    msg = {"role": "assistant", "content": content}
    msg.update(msg_extra)
    return {"choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}]}


def content_of(r):
    return r["choices"][0]["message"]["content"]


# --- the two measured cases -------------------------------------------------------------

def test_closed_think_block_is_stripped_from_content():
    """CASE 1. Before this fix the leak was returned to the caller verbatim."""
    r = coalesce_reasoning_into_content(
        resp('<think>\nOkay, the user just said "Say OK." So I should say OK.\n</think>OK'))
    assert content_of(r) == "OK"
    assert r.get("_bifrost_think_stripped") is True
    assert "<think>" not in content_of(r)


def test_unclosed_think_reports_truncation_instead_of_stop():
    """CASE 2. finish_reason lied. It now says length; the text is NOT emptied."""
    leaked = "<think>\n" + ("reasoning that never reaches an answer. " * 40)
    r = coalesce_reasoning_into_content(resp(leaked, finish_reason="stop"))
    assert r["choices"][0]["finish_reason"] == "length"
    assert r["choices"][0]["_bifrost_original_finish_reason"] == "stop"
    assert r.get("_bifrost_reasoning_truncated") is True
    assert content_of(r) == leaked, "an unclosed block must NOT be stripped to an empty 200"


# --- the boundary this fix must not cross -----------------------------------------------

def test_reasoning_only_content_is_left_intact():
    """Closed block, no answer after it. Stripping would give an empty 200 -- worse."""
    r = coalesce_reasoning_into_content(resp("<think>all reasoning, no answer</think>"))
    assert content_of(r) == "<think>all reasoning, no answer</think>"
    assert r.get("_bifrost_reasoning_only") is True
    assert "_bifrost_think_stripped" not in r


def test_multiple_closed_blocks_all_stripped():
    """replace-first would leave block 2..n behind and read as a completed fix."""
    r = coalesce_reasoning_into_content(resp("<think>a</think>first<think>b</think>second"))
    assert content_of(r) == "firstsecond"


def test_finish_reason_length_is_not_overwritten():
    """Only a lying "stop" is rewritten. An honest "length" is left as it is."""
    r = coalesce_reasoning_into_content(resp("<think>cut off", finish_reason="length"))
    assert r["choices"][0]["finish_reason"] == "length"
    assert "_bifrost_original_finish_reason" not in r["choices"][0]


# --- regressions on the behavior that already existed -----------------------------------

def test_ordinary_content_untouched():
    r = coalesce_reasoning_into_content(resp("def f():\n    return 1\n"))
    assert content_of(r) == "def f():\n    return 1\n"
    assert not any(k.startswith("_bifrost") for k in r)


@pytest.mark.parametrize("field", ["thinking", "reasoning", "reasoning_content"])
def test_empty_content_promotion_still_works(field):
    r = coalesce_reasoning_into_content(resp("", **{field: "promoted text"}))
    assert content_of(r) == "promoted text"
    assert r["_bifrost_reasoning_promoted"] is True
    assert r["_bifrost_reasoning_field"] == field


def test_malformed_response_unchanged():
    assert coalesce_reasoning_into_content({}) == {}
    assert coalesce_reasoning_into_content({"choices": []}) == {"choices": []}


def test_missing_finish_reason_does_not_crash():
    r = {"choices": [{"message": {"content": "<think>x"}}]}
    out = coalesce_reasoning_into_content(r)
    assert out.get("_bifrost_reasoning_truncated") is True
    assert "finish_reason" not in out["choices"][0]
