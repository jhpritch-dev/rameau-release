"""CEREMONY 114 (2026-10-12): the cloud egress gate's three credential holes, and the leak invariant's
first caller.

Every hole below was demonstrated on the LIVE module before it was fixed -- each let a credential cross to
a cloud tier unmasked:

  1. a bare JWT (a ServiceAccount token) matched no pattern;
  2. an assistant's tool_calls[*].function.arguments were never walked;
  3. any string already holding a `__BIFROST_` placeholder was skipped WHOLE, so a raw key beside an
     echoed placeholder crossed.

And egress_guard.assert_no_leak had zero call sites while its docstring said main.py asserted on it.

The gate under test is sanitize_messages_secrets -- the function main.py calls -- not the regexes, for
the reason test_egress_guard_bare_hex.py gives: a pattern that is not registered in _SECRET_KINDS is
inert at the boundary while still passing any regex-level test.

Run against the PREIMAGE every test here fails; that is the proof the file can see what it guards.
"""
import ast
import json
import os

import pytest

import egress_guard

KEY = "sk-" + "A" * 24
SHA1_GIT = "e912465a1b2c3d4e5f60718293a4b5c6d7e8f901"            # exactly 40: a commit hash, not a secret
SA_TOKEN = ("eyJhbGciOiJSUzI1NiIsImtpZCI6IkJLTkV5In0"
            ".eyJpc3MiOiJrdWJlcm5ldGVzL3NlcnZpY2VhY2NvdW50Iiwic3ViIjoic3lzdGVtOnNhIn0"
            ".dGhpc19pc19ub3RfYV9yZWFsX3NpZ25hdHVyZV9qdXN0X3NoYXBl")
HS_TOKEN = ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxIn0", "c2lnbmF0dXJl"])   # assembled from its parts: no JWT-shaped literal in the source


def gate(messages):
    return egress_guard.sanitize_messages_secrets(messages)


def gate_text(text):
    msgs, mapping, n = gate([{"role": "user", "content": text}])
    return msgs[0]["content"], n, mapping


# --- JWT -------------------------------------------------------------------------------------

def test_jwt_kind_is_registered_and_hex_stays_last():
    kinds = [k for k, _ in egress_guard._PATTERNS]
    assert "JWT" in kinds, "JWT missing from _PATTERNS"
    assert "JWT" in egress_guard._SECRET_KINDS, "JWT in _PATTERNS but NOT in _SECRET_KINDS: inert at the gate"
    assert kinds[-1] == "HEXSECRET", "bare hex must stay last; order is %r" % (kinds,)


@pytest.mark.parametrize("text,token", [
    ("the pod printed %s to stdout" % SA_TOKEN, SA_TOKEN),
    ("token=%s" % HS_TOKEN, HS_TOKEN),
    ("it ends a sentence: %s." % HS_TOKEN, HS_TOKEN),
])
def test_jwt_is_masked_by_the_outbound_gate(text, token):
    out, n, mapping = gate_text(text)
    assert token not in out, "a JWT crossed to cloud unmasked"
    assert n >= 1 and any(ph.startswith("__BIFROST_JWT_") for ph in mapping)


@pytest.mark.parametrize("text", [
    "eyJ",                                   # a prefix alone
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0",  # two segments
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.",  # empty signature
    "x.eyJhbGc.eyJzdWI.c2ln",                # a 4-segment run must not yield its last three
    "eyJhbGc.eyJzdWI.c2ln.ZXh0cmE",          # ...nor its first three
    "release 1.2.3 and file a.b.c",          # ordinary dotted text
])
def test_jwt_shape_negatives_are_not_masked_as_jwt(text):
    _, _, mapping = gate_text(text)
    assert not any(ph.startswith("__BIFROST_JWT_") for ph in mapping), (text, mapping)


# --- tool_calls arguments ----------------------------------------------------------------------

def _call_msg(arguments):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "run_shell", "arguments": arguments}}]}


def test_tool_call_arguments_are_masked_and_stay_valid_json():
    args = json.dumps({"cmd": "curl -H 'Authorization: token %s' https://x" % KEY})
    original = _call_msg(args)
    msgs, _, n = gate([original])
    out_args = msgs[0]["tool_calls"][0]["function"]["arguments"]
    assert KEY not in out_args, "a credential in tool_calls arguments crossed unmasked"
    assert n >= 1
    json.loads(out_args)                                     # still valid JSON
    assert original["tool_calls"][0]["function"]["arguments"] == args, "the gate mutated its input"
    assert msgs[0]["tool_calls"][0]["id"] == "call_1"        # structure untouched


def test_tool_call_without_a_secret_is_returned_as_is():
    m = _call_msg(json.dumps({"path": "README.md"}))
    msgs, _, n = gate([m])
    assert n == 0 and msgs[0] is m


# --- the placeholder skip ----------------------------------------------------------------------

def test_an_echoed_placeholder_does_not_shield_a_raw_key():
    out, n, _ = gate_text("earlier you said __BIFROST_SECRET_1__; my new key is %s" % KEY)
    assert KEY not in out, "a raw key beside a placeholder crossed unmasked"
    assert "__BIFROST_SECRET_1__" in out and n >= 1


@pytest.mark.parametrize("text", [
    "key %s and Authorization: Bearer abcdefghijklmnopqrstuvwx" % KEY,  # rameau-scan: allow -- synthetic bearer fixture: the leak report must redact it
    "jwt %s, sha %s" % (HS_TOKEN, "f" * 64),
])
def test_a_second_pass_is_a_no_op(text):
    once, n1, _ = gate_text(text)
    twice, n2, m2 = gate_text(once)
    assert n1 >= 1
    assert twice == once and n2 == 0 and m2 == {}, "re-scanning masked text changed it: %r" % twice


def test_an_echoed_bearer_placeholder_is_not_wrapped_again():
    """What BEARER's (?!__BIFROST_) buys: an echoed masked header passes through UNCHANGED, rather than
    being wrapped into a second placeholder whose 'original' is itself a placeholder."""
    text = "Authorization: Bearer __BIFROST_SECRET_1__ was what you sent"
    out, n, mapping = gate_text(text)
    assert out == text and n == 0 and mapping == {}, (out, mapping)


# --- the report --------------------------------------------------------------------------------

def test_report_is_clean_after_the_gate():
    msgs, mapping, _ = gate([{"role": "user", "content": "use %s" % KEY}, _call_msg(json.dumps({"k": KEY}))])
    assert egress_guard.outbound_leak_report(msgs, mapping) == {"leaked": 0, "residual": {}}


def test_report_counts_a_masked_original_that_survives_in_an_unwalked_field():
    msgs, mapping, _ = gate([{"role": "user", "content": "use %s" % KEY}])
    msgs = msgs + [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x?k=" + KEY}}]}]
    rep = egress_guard.outbound_leak_report(msgs, mapping)
    assert rep["leaked"] == 1 and rep["residual"] == {"SECRET": 1}, rep


def test_report_counts_a_token_the_gate_never_saw():
    msgs = [{"role": "user", "name": KEY, "content": "hello"}]
    rep = egress_guard.outbound_leak_report(msgs, {})
    assert rep == {"leaked": 0, "residual": {"SECRET": 1}}, rep


def test_report_never_carries_a_value():
    msgs = [{"role": "user", "name": KEY, "content": "hello"}]
    rep = egress_guard.outbound_leak_report(msgs, {"__BIFROST_SECRET_1__": KEY})
    assert KEY not in repr(rep)


def test_report_does_not_flag_a_git_sha1():
    rep = egress_guard.outbound_leak_report([{"role": "user", "name": SHA1_GIT, "content": "x"}], {})
    assert rep == {"leaked": 0, "residual": {}}, rep


def test_report_on_nothing_is_clean():
    assert egress_guard.outbound_leak_report([], {}) == {"leaked": 0, "residual": {}}


# --- main.py calls it, in the cloud branch, after the gate --------------------------------------

def _dispatch_to_tier_src():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "main.py"), encoding="utf-8").read()
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "dispatch_to_tier":
            return node, text
    raise AssertionError("main.py has no async def dispatch_to_tier")


def test_main_calls_the_report_in_the_cloud_branch_after_the_gate():
    fn, _ = _dispatch_to_tier_src()
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and "is_cloud_tier_value" in ast.dump(node.test):
            calls = [ast.dump(c.func) for c in ast.walk(ast.Module(body=node.body, type_ignores=[]))
                     if isinstance(c, ast.Call)]
            gate_i = next((i for i, c in enumerate(calls) if "sanitize_messages_secrets" in c), None)
            rep_i = next((i for i, c in enumerate(calls) if "outbound_leak_report" in c), None)
            assert gate_i is not None, "the cloud branch no longer calls the gate"
            assert rep_i is not None, "the cloud branch never calls outbound_leak_report"
            assert rep_i > gate_i, "the report must run AFTER the gate"
            return
    raise AssertionError("no `if ... is_cloud_tier_value(...)` block in dispatch_to_tier")


def test_main_never_logs_the_mapping():
    """No log call anywhere in dispatch_to_tier may reference _leak_map: it holds the raw values. Checked on
    the whole call's AST, so an f-string split over several lines cannot hide it."""
    fn, _ = _dispatch_to_tier_src()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "log"):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            assert "_leak_map" not in names, "log.%s(...) formats the mapping" % node.func.attr
