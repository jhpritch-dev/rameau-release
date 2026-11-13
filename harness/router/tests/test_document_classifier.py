"""
Regression guard for the AUTOPILOT document/code classifier.

Through v215 `_is_document_task` carried bare alternatives (`write a`, `draft a`,
`brief`, `report`, `document`) alongside its correctly-anchored `generate a ...`
branch. Consequence, measured live on graph 94957da1: "Write a Python function..."
classified as DOCUMENT, went to the document decomposer, and assembled a prose
essay about CSV parsing instead of code. The inverse prompt ("Summarize the...")
classified as CODE. Nothing tested this, so it inverted silently.

`_is_prose_task` unions the same detector, so a classifier miss corrupts both the
decompose side and the assemble side. Both are asserted here.
"""
import pytest

from autopilot_graph import _is_document_task, _is_prose_task

# The two demo-shaped prompts that were inverted live. These are the regression.
DEMO_CODE = (
    "Write a Python function to parse a CSV file and return the sum of a "
    "numeric column, with a unit test."
)
DEMO_DOC = "Summarize the tradeoffs of local LLM inference vs cloud APIs."

DOCUMENT_PROMPTS = [
    DEMO_DOC,
    "Write a whitepaper on local inference economics.",
    "Draft a brief on fleet topology.",
    "Generate a report on routing latency.",
    "Produce an executive summary of Q3 results.",
    "Write a technical guide to our routing tiers.",
    "Summarize the overnight training results.",
    "Author a memo about the deploy freeze.",
    "Create a primer on tier-based routing.",
    "Write an article about GPU scheduling.",
]

CODE_PROMPTS = [
    DEMO_CODE,
    "Implement a binary search in Python.",
    "Write a function that reverses a linked list.",
    "Refactor this class to use dependency injection.",
    # Bare doc nouns inside a code request -- each of these was a false DOCUMENT.
    "Refactor the document parser.",
    "Fix the report generator class.",
    "Write a Python script to generate a summary report.",
    "Add a section to the config parser module.",
    "Write a CLI that reports disk usage.",
    "Create a module that documents API endpoints.",
    "Debug the failing pytest suite.",
]


@pytest.mark.parametrize("prompt", DOCUMENT_PROMPTS)
def test_document_prompts_classify_as_document(prompt):
    assert _is_document_task(prompt) is True, f"should be DOCUMENT: {prompt!r}"


@pytest.mark.parametrize("prompt", CODE_PROMPTS)
def test_code_prompts_classify_as_code(prompt):
    assert _is_document_task(prompt) is False, f"should be CODE: {prompt!r}"


def test_demo_code_prompt_is_not_document():
    """The exact prompt that assembled a prose essay on graph 94957da1."""
    assert _is_document_task(DEMO_CODE) is False


def test_demo_doc_prompt_is_document():
    assert _is_document_task(DEMO_DOC) is True


# _is_prose_task unions _is_document_task, so the assembler must agree with the
# decomposer. Disagreement is the "prose sections wrapped in a Python module" bug.
def test_assembler_agrees_code_prompt_is_not_prose():
    assert _is_prose_task(DEMO_CODE) is False


def test_assembler_agrees_doc_prompt_is_prose():
    assert _is_prose_task(DEMO_DOC) is True


# --- _is_prose_task bare-signal false positives (graph d2445533, 2026-07-22) --------------
# _is_prose_task scanned _PROSE_SIGNALS as bare substrings with NO code veto, so a code
# deliverable that merely mentioned a prose noun ("summary", "report", "overview", "document")
# routed to the prose assembler and came back a "## s1"-labeled tutorial instead of a module.
# _is_document_task already vetoes these (via _CODE_SIGNALS); _is_prose_task now does too.
# The exact demo prompt that broke: "outputs summary statistics" (has "summary", asks for a CLI).
PROSE_FALSE_POSITIVE_CODE = [
    "Build a Python CLI tool that reads a CSV and outputs summary statistics, "
    "with error handling and a --help flag.",
    "Write a Python script to generate a summary report.",
    "Add a section to the config parser module.",
    "Create a module that documents API endpoints.",
    "Implement a function that returns a summary dict.",
    "Refactor the report generator class.",
]


@pytest.mark.parametrize("prompt", PROSE_FALSE_POSITIVE_CODE)
def test_code_prompt_with_prose_noun_is_not_prose(prompt):
    """A code deliverable that merely mentions a prose noun must not route to the prose assembler."""
    assert _is_prose_task(prompt) is False, f"code prompt wrongly classified prose: {prompt!r}"


def test_genuine_doc_mentioning_code_stays_prose():
    """The new _CODE_SIGNALS veto on the bare-signal branch must NOT flip a real document
    request that names code. 'executive summary' is an unambiguous artifact matched by
    _is_document_task's STRONG group (no authoring verb, so the weak+veto path is not taken),
    so the union keeps it prose even though 'function' is present."""
    assert _is_prose_task("An executive summary of the parse_csv function tradeoffs.") is True


@pytest.mark.parametrize("bad", [None, "", "   ", 42, [], {}])
def test_non_string_and_empty_are_not_documents(bad):
    assert _is_document_task(bad) is False
