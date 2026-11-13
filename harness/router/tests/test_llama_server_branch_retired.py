"""main.dispatch_to_tier's llama_server branch fails by name instead of calling a removed provider.

2026-09-11, Igor review follow-up (F-2 in PENDING-router-tool-loop-backend-recovery, "Findings,
not fixed here"). The branch called openai_compat_chat_completion(provider="llamaserver"). That
PROVIDER_DEFAULTS entry was removed in v154, so taking the branch raised "Unknown provider" from
inside openai_compat. No tier uses the type. main.py and openai_compat.py are read as SOURCE
(ast), not imported.
"""
import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MAIN = _ROOT / "main.py"


def _dispatch_fn():
    tree = ast.parse(_MAIN.read_text(encoding="utf-8"))
    fns = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "dispatch_to_tier"]
    assert len(fns) == 1
    return fns[0]


def _llama_branch():
    for node in ast.walk(_dispatch_fn()):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            t = node.test
            if (isinstance(t.left, ast.Name) and t.left.id == "backend_type"
                    and len(t.comparators) == 1 and isinstance(t.comparators[0], ast.Constant)
                    and t.comparators[0].value == "llama_server"):
                return node
    raise AssertionError("no `backend_type == 'llama_server'` branch in dispatch_to_tier")


def test_branch_raises_valueerror_by_name():
    raises = [n for s in _llama_branch().body for n in ast.walk(s) if isinstance(n, ast.Raise)]
    assert raises, "the llama_server branch must raise"
    exc = raises[0].exc
    assert isinstance(exc, ast.Call) and getattr(exc.func, "id", None) == "ValueError"
    text = ast.unparse(exc)
    assert "retired" in text and "llama_server" in text


def test_branch_no_longer_calls_the_removed_provider():
    src = "\n".join(ast.unparse(s) for s in _llama_branch().body)
    assert "llamaserver" not in src
    assert "openai_compat_chat_completion" not in src


def test_other_backend_branches_untouched():
    """The ollama / openai_compat / anthropic / n8n branches and the final else still exist."""
    src = ast.unparse(_dispatch_fn())
    for needle in ("backend_type == 'ollama'", "backend_type == 'anthropic'",
                   "backend_type == 'n8n_workflow'", "backend_type == 'openai_compat'",
                   "Unsupported backend type"):
        assert needle in src, needle


def test_the_provider_really_is_gone():
    """The reason for the change. If someone restores the provider, revisit this branch."""
    tree = ast.parse((_ROOT / "backends" / "openai_compat.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "PROVIDER_DEFAULTS" for t in node.targets):
            keys = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
            assert keys and "llamaserver" not in keys
            return
    raise AssertionError("PROVIDER_DEFAULTS not found")


def test_no_tier_backend_uses_llama_server():
    import config
    assert Path(config.__file__).resolve().parent == _ROOT
    types = {(b or {}).get("type") for b in config.TIER_BACKENDS.values()}
    assert "llama_server" not in types, types
