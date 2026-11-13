"""Prompt guards for the test-leg system prompt.

NOTE ON THE FILENAME: "stdlib_runner" is now a misnomer and is kept only so this file's
history stays in one place. The stdlib runner it was named for was retired on 2026-07-31,
when execsvc became bifrost-aio-sandbox:v5 and started running REAL pytest. Renaming is a
separate, mechanical change.

WHAT THIS FILE USED TO ASSERT, AND WHY IT WAS REMOVED (#151, 2026-08-16):

    test_test_leg_prompt_never_demands_a_pytest_fixture   -- DELETED. Fixtures are injected.
    test_test_leg_prompt_teaches_the_zero_argument_rule   -- DELETED. Tests may take fixtures.

Both were correct when written (2026-07-27, after the v264 bank) and false from 2026-07-31.
They are the reason #149 shipped a wrong "fix": the corpus agreed with the stale comment, so
a green suite confirmed a contract the grader had already abandoned. A guard that encodes a
claim about an external service has to be re-derived from that service, not from the source
that reads it.

Probed live 2026-08-16 (POST /execute on execsvc, from inside the router pod):
    tmp_path / capsys / monkeypatch -> injected, passes      unknown fixture -> ERROR (failure)
    parametrize -> 3 collected                               pytest.skip     -> FAILURE (policy)
    unittest / plain Test classes -> collected               `__main__`-only -> 0 collected

The surviving guards below are the two that do NOT depend on which runner is in use, plus a
replacement for the deleted pair. Contract parity between the test-bearing arms lives in
tests/test_runner_contract_parity.py.
"""
import re

import pytest

from bifrost_message import SubtaskSpec
from subtask_graph import _build_messages

_FIXTURES = ("capsys", "tmp_path", "monkeypatch", "capfd", "caplog")


def _test_leg(contract=False):
    return SubtaskSpec(
        type="subtask_spec",
        graph_id="g",
        subtask_id="s2",
        description="Provide a pytest test suite for the CLI implementation.",
        acceptance_criteria=["ac"],
        assigned_tier="2_qwen_coder",
        deliverable="tests",
        contract_dep="s1" if contract else None,
    )


def _prompt_text(spec):
    return "\n\n".join(m.get("content", "") for m in _build_messages(spec, {}))


def test_test_leg_prompt_teaches_the_import_scope_rule():
    """Replaces the deleted zero-argument guard.

    Import scope is the real structural way to collect zero tests, and it is runner-agnostic:
    a `__main__`-guarded test is invisible to any collector. Measured on the v352 bank -- of
    the 5 rejected bodies the log carries verbatim, 5 sat under a `__main__` guard.
    """
    for contract in (False, True):
        text = _prompt_text(_test_leg(contract=contract))
        assert "at MODULE level" in text, (
            "contract=%s: nothing warns the leg that `__main__`-scoped tests never run"
            % contract
        )
        assert "__main__" in text


def test_test_leg_prompt_does_not_forbid_a_real_fixture():
    """The inverse of the guard this file used to carry.

    #149 removed correct `tmp_path` advice on the strength of a stale comment. Pin the
    correction so the ban cannot come back by habit.
    """
    for contract in (False, True):
        text = _prompt_text(_test_leg(contract=contract))
        for phrase in ("never `tmp_path`", "NEVER injected", "ZERO parameters",
                       "there are no pytest fixtures"):
            assert phrase not in text, (
                "contract=%s: prompt still forbids or denies %r, but execsvc injects fixtures"
                % (contract, phrase)
            )


def test_test_leg_prompt_teaches_a_capture_idiom():
    """Runner-agnostic: however output is captured, the leg must be told to capture it.

    The v264 defect was asserting on output that was never captured at all. Either idiom
    (`capsys` or `redirect_stdout`) satisfies this; naming one specifically does not.
    """
    for contract in (False, True):
        text = _prompt_text(_test_leg(contract=contract))
        assert ("capsys" in text) or ("redirect_stdout" in text), (
            "contract=%s: no way to capture printed output is offered" % contract
        )


def test_prompt_still_permits_pytest_raises():
    """Unchanged and still true: `pytest.raises` is available and legitimate."""
    for contract in (False, True):
        text = _prompt_text(_test_leg(contract=contract))
        assert "pytest.raises" in text
        assert "import pytest" in text


def test_contract_preamble_does_not_restate_the_runner_contract_independently():
    """Unchanged in intent and still the load-bearing one.

    v264 proved a second copy of the contract is where the two drift apart -- and it drifted
    again: until #151 the preamble still claimed there are no fixtures, ~2 weeks after the
    switch to real pytest. Keep exactly one source of truth.
    """
    msgs = _build_messages(_test_leg(contract=True), {})
    preamble = "\n".join(m.get("content", "") for m in msgs if m.get("role") == "user")
    # Strip comment lines: a comment EXPLAINING the rule is not a restatement of it.
    preamble = "\n".join(l for l in preamble.splitlines() if not l.strip().startswith("#"))
    for fixture in _FIXTURES:
        assert fixture not in preamble, (
            "the contract preamble names `%s` -- the runner contract belongs in ONE place, "
            "the system prompt" % fixture
        )
    assert not re.search(r"ZERO parameters|no pytest fixtures", preamble)
