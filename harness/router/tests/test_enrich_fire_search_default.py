"""The SearXNG helper's default must stay fail-safe, and no caller may rely on it.

v235 (2026-07-18) made confidence authoritative on /v1/chat/completions: web search fires only on an
explicit X-Web-Search: true, or a web-intent query below the confidence threshold. The shared helper
`_enrich_messages_with_rag` defaulted `fire_search=True` and its docstring claimed that default
"matches /v1/chat/completions behavior", which stopped being true at v235.

Nothing guarded any of this, and the gap cost a wrong analysis on 2026-09-26: reading the True default
plus the stale docstring, I reported that the /v1/messages shim fired SearXNG unconditionally and called
it a live prompt-injection surface. It does not -- its one caller passes fire_search=False explicitly.

`test_every_caller_passes_fire_search_explicitly` is that lesson as a check: it fails the moment a caller
leans on the default, which is the only way the default could ever become behavior.
"""
import ast
import inspect
import io

import main


def _main_source():
    return io.open(inspect.getsourcefile(main), encoding="utf-8").read()


def _calls_to(tree, name):
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            fname = getattr(f, "id", None) or getattr(f, "attr", None)
            if fname == name:
                out.append(node)
    return out


def _funcdef(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def test_default_is_fail_safe():
    """Read the signature, not the source text: the default is what callers actually get."""
    sig = inspect.signature(main._enrich_messages_with_rag)
    assert "fire_search" in sig.parameters
    assert sig.parameters["fire_search"].default is False, (
        "fire_search must default to False. A future caller that omits it would otherwise fire web "
        "search against the v235 policy that confidence is authoritative.")


def test_every_caller_passes_fire_search_explicitly():
    """The default must never be load-bearing -- this is the check that was missing."""
    tree = ast.parse(_main_source())
    calls = _calls_to(tree, "_enrich_messages_with_rag")
    assert calls, "no call site for _enrich_messages_with_rag found at all"
    missing = []
    for c in calls:
        kwargs = {k.arg for k in c.keywords if k.arg}
        if "fire_search" not in kwargs:
            missing.append(c.lineno)
    assert not missing, (
        "these call sites rely on the fire_search DEFAULT instead of stating their intent: %s. "
        "State it explicitly -- a default plus a docstring is a declaration, not behavior."
        % missing)


def test_the_parameter_is_still_honoured():
    """A fail-safe default is worthless if the flag no longer gates the search."""
    tree = ast.parse(_main_source())
    fn = _funcdef(tree, "_enrich_messages_with_rag")
    assert fn is not None
    guarded = False
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) \
                and node.test.id == "fire_search":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    f = inner.func
                    if (getattr(f, "id", None) or getattr(f, "attr", None)) == "_searxng_search":
                        guarded = True
    assert guarded, "_searxng_search is no longer guarded by `if fire_search:`"


def test_the_docstring_no_longer_claims_parity_with_chat_completions():
    doc = inspect.getdoc(main._enrich_messages_with_rag) or ""
    assert "Default True" not in doc, "the docstring still advertises a True default"
    assert "matches" not in doc or "/v1/chat/completions behavior" not in doc, (
        "the docstring still claims the default matches /v1/chat/completions, which has gated "
        "web search since v235")
    assert "fail-safe" in doc.lower(), "the docstring should say why the default is False"


def test_the_v104_bypass_claim_is_no_longer_stated_as_current():
    """The stale comment that was cited as evidence for a conclusion it did not support."""
    src = _main_source()
    assert "# v104: also triggers on detected web_search intent (bypasses confidence/band/code gates)" \
        not in src, "the v104 bypass comment is back, stated as current behavior"
    assert "v235" in src, "the v235 reversal should be named where the v104 claim used to sit"


def test_the_confidence_gate_itself_is_still_there():
    """Two-sided: the comment correction is only honest if the gate it describes exists."""
    src = _main_source()
    assert "_explicit_web" in src
    assert "_fire_web" in src
    assert "search_confidence_threshold" in src
    tree = ast.parse(src)
    assigns = [n for n in ast.walk(tree)
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "_fire_web" for t in n.targets)]
    assert assigns, "_fire_web is no longer assigned -- the gate the comments describe is gone"
