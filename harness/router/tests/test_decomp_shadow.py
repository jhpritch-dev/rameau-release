"""Tests for the Phase 0c shadow decomposition heuristic (main._would_decompose).

Observation-only signal; this just pins the crude heuristic's behavior.
"""
import main


def test_numbered_list_decomposes():
    assert main._would_decompose("1. clone the repo\n2. run the tests") is True


def test_sequencing_cues_decompose():
    assert main._would_decompose("build the API then write the tests") is True
    assert main._would_decompose("scaffold it and then deploy") is True
    assert main._would_decompose("write the loader; after that wire it in") is True
    assert main._would_decompose("draft the doc and finally publish") is True


def test_two_imperatives_decompose():
    assert main._would_decompose("create a function and add logging to it") is True


def test_single_imperative_no_cue_does_not_decompose():
    assert main._would_decompose("create a function that reverses a string") is False


def test_plain_question_does_not_decompose():
    assert main._would_decompose("what is the capital of France") is False


def test_empty_does_not_decompose():
    assert main._would_decompose("") is False
    assert main._would_decompose(None) is False


def test_then_substring_false_positive_guard():
    # "strengthen" contains the substring "then" but must NOT trip the cue (\bthen\b),
    # and "strengthen" is not an imperative in the list -> no decomposition.
    assert main._would_decompose("strengthen the password hashing") is False
