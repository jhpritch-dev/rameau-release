"""#151 -- every test-bearing deliverable is graded by the SAME service, so every one of them
must be told the same TRUE facts about it.

This file replaces the #149 version, which pinned a contract that was already false. History,
because it is the whole point of the tripwire below:

    2026-07-16/23  execsvc runs a stdlib runner + pytest_shim: no fixture injection.
    2026-07-27     subtask_graph's `tests` arm documents that, correctly, at length.
    2026-07-31     OPERATOR DECISION: execsvc becomes bifrost-aio-sandbox:v5 and runs REAL
                   pytest. pytest_shim.py retired. The prompt text is now FALSE.
    2026-08-16     #149 reads the (stale) source comment, concludes the correct `tmp_path`
                   advice added in #145 is a defect, removes it, and COPIES the false claims
                   into the code_selftest arm. Shipped as v352, with 11 tests pinning it.
    2026-08-16     #151 probes the live service and corrects all of it.

MEASURED against the live grader (POST /execute from inside the router pod, 2026-08-16):

    def test_x(tmp_path) / (capsys) / (monkeypatch)  -> injected, 1 collected, passes
    @pytest.mark.parametrize('n', [1,2,3])           -> 3 collected, passes
    unittest.TestCase subclass, plain `class Test`   -> collected, passes
    def test_x(not_a_real_fixture)                   -> ERROR -> counted as a FAILURE
    pytest.raises(...) without `import pytest`       -> NameError -> FAILS
    pytest.skip(...)                                 -> counted as a FAILURE (bifrost policy,
                                                        kept deliberately from the shim era)
    tests under `if __name__ == "__main__":`         -> collected=0 -> "NO TESTS PRESENT"

So there are exactly two ways to collect zero: hide the tests from import, or write none.
Those are what the prompt must warn about.

The tests below therefore assert (a) parity between the two arms that share a grader, and
(b) that no arm re-acquires a stale claim. (b) is the one that matters: presence-checking is
how the previous corpus passed while describing a grader that no longer existed.
"""
import pytest

from bifrost_message import SubtaskSpec
from subtask_graph import _build_messages

# Both are in _TEST_BEARING_DELIVERABLES and both are graded by real pytest in execsvc.
TEST_BEARING = ["tests", "code_selftest"]

FILE_DESC = ("Write a command-line tool that reads a CSV file and prints summary statistics "
             "for each numeric column")

# Claims that were true under the shim and are FALSE against bifrost-aio-sandbox:v5.
# If one of these reappears in a prompt, someone has trusted a comment over the service.
STALE_CLAIMS = [
    "ZERO parameters",
    "NEVER injected",
    "never `tmp_path`",
    "there are no pytest fixtures",
    "this runner injects none",
    "stdlib-only",
    "SKIPPED -- takes arguments",
]


def _prompt(deliverable, description=FILE_DESC):
    spec = SubtaskSpec(
        type="subtask_spec",
        graph_id="g-151",
        subtask_id="s1",
        description=description,
        acceptance_criteria=["ac"],
        assigned_tier="2_qwen_coder",
        deliverable=deliverable,
    )
    msgs = _build_messages(spec, {})
    return "\n\n".join(m.get("content", "") for m in msgs)


@pytest.mark.parametrize("deliverable", TEST_BEARING)
def test_import_scope_rule_reaches_every_test_bearing_leg(deliverable):
    """The ONLY structural way to collect zero tests, measured on 5 of 5 carried v352 bodies."""
    text = _prompt(deliverable)
    # Key on the substance, not one arm's exact phrasing -- the two arms word it
    # differently because the `tests` arm is pinned against the envelope gate and has to
    # say it in fewer characters.
    assert "at MODULE level" in text, (
        "%s can lose the whole leg to a __main__ guard and is never told so" % deliverable
    )
    assert "__main__" in text
    assert "NOT collected" in text


@pytest.mark.parametrize("deliverable", TEST_BEARING)
def test_import_rule_reaches_every_test_bearing_leg(deliverable):
    """`pytest.raises` without `import pytest` is a NameError -- probed live, and measured
    twice in the v351 bank. This is the one half of #149 that was correct."""
    text = _prompt(deliverable)
    assert "import pytest" in text, (
        "%s may use pytest.raises but is never told to import pytest" % deliverable
    )


@pytest.mark.parametrize("deliverable", TEST_BEARING + ["code"])
@pytest.mark.parametrize("claim", STALE_CLAIMS)
def test_no_arm_repeats_a_shim_era_claim(deliverable, claim):
    """THE tripwire. Each of these was true before 2026-07-31 and is false now.

    Do not 'fix' a failure here by editing the string list. Probe the live service
    (POST /execute on execsvc) and make the prompt say what it answers.
    """
    assert claim not in _prompt(deliverable), (
        "%s repeats the shim-era claim %r; execsvc has run REAL pytest since 2026-07-31"
        % (deliverable, claim)
    )


@pytest.mark.parametrize("deliverable", TEST_BEARING)
def test_fixtures_are_not_forbidden(deliverable):
    """#149 removed correct advice. Guard the reversal: nothing may ban a real fixture."""
    text = _prompt(deliverable)
    for banned in ("Do NOT ask for `tmp_path`", "never `tmp_path`", "avoid fixtures"):
        assert banned not in text, (
            "%s forbids %r, but the live grader injects it" % (deliverable, banned)
        )


def test_the_file_task_pattern_is_one_the_grader_can_actually_run():
    """A file/CLI selftest leg must be given a workable way to make its input."""
    text = _prompt("code_selftest")
    assert "TESTING A FILE / CLI TASK" in text
    assert "io.StringIO" in text
    assert 'open("d.csv", "w")' in text or "open('d.csv', 'w')" in text


def test_a_plain_code_leg_gets_none_of_this():
    """The implementation-only arm writes no tests, so grader facts would be wasted envelope.

    subtask_graph.py is explicit that _SYSTEM_PROMPT_RESERVE_TOKENS pays for this text and
    test_dep_context's envelope gate fails a leg if the system prompt outgrows it.
    """
    text = _prompt("code")
    assert "Do NOT write test functions" in text
    assert "at MODULE level" not in text


def test_selftest_prompt_stays_smaller_than_the_tests_prompt():
    """Envelope tripwire: the selftest arm may grow, but should not overtake the tests arm.

    Not a law of nature. If this fails, check the envelope gate deliberately rather than
    raising the number.
    """
    selftest = len(_prompt("code_selftest"))
    tests = len(_prompt("tests"))
    assert selftest < tests, (
        "code_selftest prompt (%d) has overtaken the tests prompt (%d); re-check the "
        "_SYSTEM_PROMPT_RESERVE_TOKENS envelope before accepting this" % (selftest, tests)
    )
