"""B3 docs-example verification (#138) -- extraction and the report-only guarantee.

Every fixture below is a VERBATIM excerpt from an archived docs-leg body (v346 bank-138a and
v347 bank-v347, EVIDENCE-docs-bodies-*.jsonl). They are not invented shapes: the whole reason B3
exists in this form is that invented shapes are what failed. 4d5170e enumerated the forms a
stated output could take, and the model kept the semantics while changing the phrasing.

The negative cases carry the weight:
  * a bare `f()` with the input named in prose must yield NO claim -- guessing the input would
    manufacture a verdict out of nothing
  * ordinary prose that merely looks like a call must not be able to FAIL a leg
"""
import re

from autopilot_graph import _b3_build_check, _b3_extract_claims

# v347 take 1, verbatim. The prose form that 4d5170e's enumeration did not cover.
V347_PROSE = (
    'word_frequencies(text: str) is a function that takes a string as input and returns a '
    'dictionary mapping each unique word to the number of times it appears in the text. For '
    'example, `word_frequencies("hello world hello again")` would return '
    "`{'hello': 2, 'world': 1, 'again': 1}`.\n\n"
    'top_n_words(text: str, n: int) is a function that takes a string and an integer as input. '
    'In case of ties, it breaks them alphabetically. For example, '
    '`top_n_words("hello world hello again", 2)` would return '
    "`[('hello', 2), ('world', 1)]`."
)

# v346 take 2, verbatim -- the older comment form, inside a fence.
V346_COMMENT = (
    '```python\n'
    'word_frequencies("Hello world! Hello again, world!")\n'
    "# Output: {'Hello': 2, 'world': 2, '!': 1, ' ': 4}\n"
    '```\n'
)

# v347 take 2, verbatim -- the body that must NOT be gated. The call has no arguments; the input
# is named separately in prose, so there is nothing executable to extract.
V347_BARE_CALL = (
    '`word_frequencies(text: str)`: This function returns a dictionary mapping each unique word '
    'in the input text to the number of times it occurs. For example, given the text '
    '"Hello world! Hello again, world!", calling `word_frequencies()` would yield:\n'
    '```python\n'
    "{'hello': 2, 'world': 2, 'again': 1}\n"
    '```\n'
)


def test_extracts_the_prose_form_that_defeated_the_enumeration():
    claims = _b3_extract_claims(V347_PROSE)
    got = {(f, a) for f, a, _v in claims}
    assert ('word_frequencies', '"hello world hello again"') in got
    assert ('top_n_words', '"hello world hello again", 2') in got
    vals = {v for _f, _a, v in claims}
    assert "{'hello': 2, 'world': 1, 'again': 1}" in vals


def test_extracts_the_older_comment_form():
    claims = _b3_extract_claims(V346_COMMENT)
    assert any(f == 'word_frequencies' and "' ': 4" in v for f, _a, v in claims)


def test_bare_call_yields_no_claim():
    """The 1 of 6 archived bodies that is correctly NOT GATED."""
    assert [c for c in _b3_extract_claims(V347_BARE_CALL)
            if c[0] == 'word_frequencies' and c[1].strip()] == []


def test_a_type_annotation_is_not_a_claim():
    """`dict[str, int]` is what the leg legitimately knows. It must never be read as a value."""
    assert _b3_extract_claims(
        'word_frequencies(text: str) -> dict[str, int] returns a mapping of words to counts.'
    ) == []


def test_prose_in_braces_is_not_a_claim():
    assert _b3_extract_claims(
        '`f("x")` would return `{the word counts}` for the input.'
    ) == []


def test_check_module_guards_every_call_so_a_bad_extraction_skips():
    """Measured 2 of 12 on the archived corpus: prose 'ties broken alphabetically
    (lexicographically)' parses as a call. An undefined name means the EXTRACTOR was wrong, so
    the generated test must SKIP -- never fail the leg on the extractor's mistake."""
    src = _b3_build_check('IMPL', [('alphabetically', 'lexicographically', '[1]')])
    assert 'skip' in src
    assert "globals().get('alphabetically')" in src
    # the assertion must sit AFTER the guard, or an undefined name raises NameError instead
    assert src.index('skip') < src.index('assert')


def test_check_module_is_appended_as_a_fenced_block_after_the_artifact():
    src = _b3_build_check('ASSEMBLED-ARTIFACT', [('f', '1', '2')])
    # The artifact is FENCED, and that is the fix, not a regression. Before 446e7ad
    # (PACKET-68 fix 1) _b3_build_check appended its fenced test block to an UNFENCED
    # artifact; execsvc joins only fenced blocks, so the implementation was dropped and
    # every claim skipped -- measured "impl UNFENCED -> collected=1 run=0 failed=1".
    # This assertion used to require a bare artifact, i.e. it asserted the old bug.
    assert src.startswith('```python\nASSEMBLED-ARTIFACT\n```')
    assert '```python' in src and src.rstrip().endswith('```')
    # PACKET-69 s3.1 (1631b0e) rewrote the generated check to recover the ACTUAL value:
    # `_v = _f(1)` then an assertion carrying it, so a failed claim reports what the
    # function really returned instead of only that it mismatched. The old
    # single-expression form no longer appears.
    assert '_v = _f(1)' in src
    assert "assert _v == 2, 'B3_ACTUAL_0=%r'" in src


def test_report_only_the_call_site_consumes_nothing():
    """The report-only guarantee, checked structurally rather than trusted.

    _b3_report's verdict must not be assigned, branched on, or fed to the review trigger. If a
    later edit wires it in, that is a deliberate decision (STAGE section 6) and this test should
    be changed in the same commit that makes it.
    """
    import inspect

    import autopilot_graph
    src = inspect.getsource(autopilot_graph)
    calls = re.findall(r'^.*_b3_report\(.*$', src, re.M)
    invocations = [ln for ln in calls if 'async def' not in ln]
    assert len(invocations) == 1, invocations
    line = invocations[0].strip()
    assert line.startswith('await _b3_report('), line
    assert '=' not in line.split('_b3_report(')[0], 'verdict is being captured: %s' % line


# v348 bank, demobank-2, VERBATIM. This prose matched as a call and generated a SyntaxError,
# so execsvc reported "compile FAIL" for the whole check module and that take's TWO legitimate
# claims were never evaluated. Found by B3's first live bank, one hour after B3 shipped.
V348_PROSE_THAT_LOOKS_LIKE_A_CALL = (
    'top_n_words returns the n most frequent words, with ties broken alphabetically '
    '(i.e., the lexicographically smallest words have higher rank), for example '
    "`top_n_words(\"Hello world! Hello again world.\", 2)` would return "
    "`[('world', 2), ('Hello', 2)]`."
)


def test_an_unparseable_call_is_dropped_not_emitted():
    """The failure mode the runtime guard CANNOT catch: globals().get() only runs if the module
    compiles, and an unparseable argument list means it does not."""
    claims = _b3_extract_claims(V348_PROSE_THAT_LOOKS_LIKE_A_CALL)
    assert all(f != 'alphabetically' for f, _a, _v in claims), claims


def test_the_legitimate_claim_beside_it_still_survives():
    """One bad extraction must not take the good ones with it -- that is what happened live."""
    claims = _b3_extract_claims(V348_PROSE_THAT_LOOKS_LIKE_A_CALL)
    assert any(f == 'top_n_words' for f, _a, _v in claims), claims


def test_every_emitted_claim_produces_a_compilable_check_module():
    """The invariant, stated once: whatever the extractor returns, the generated module must
    compile. Anything else can silently void an entire take's verification."""
    bodies = [V347_PROSE, V346_COMMENT, V347_BARE_CALL, V348_PROSE_THAT_LOOKS_LIKE_A_CALL]
    for body in bodies:
        claims = _b3_extract_claims(body)
        if not claims:
            continue
        src = _b3_build_check('', claims)
        fenced = src.split('```python')[1].rsplit('```', 1)[0]
        compile(fenced, '<b3-check>', 'exec')
