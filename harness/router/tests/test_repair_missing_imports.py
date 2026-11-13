"""A5: unit tests for _repair_missing_imports (6 cases from the stage doc). ASCII-only."""
from subtask_graph import _repair_missing_imports, _undefined_called_names


def _fence(code: str) -> str:
    return "```python\n" + code + "\n```"


def _undef_of(text: str):
    import subtask_graph as sg
    code = "\n\n".join(b.strip() for b in sg._FENCE_PY_RE.findall(text) if b.strip())
    return _undefined_called_names(code)


def test_1_injects_import_os():
    t = _fence("def main():\n    return os.getcwd()")
    out, n = _repair_missing_imports(t)
    assert n == 1, n
    assert "import os" in out
    assert _undef_of(out) == [], _undef_of(out)


def test_2_injects_typing_list():
    t = _fence("def f(xs: List[int]) -> List[int]:\n    return xs")
    out, n = _repair_missing_imports(t)
    assert n == 1, n
    assert "from typing import List" in out


def test_3_user_helper_not_repaired():
    t = _fence("def main(x):\n    return validate_path(x)")
    out, n = _repair_missing_imports(t)
    assert n == 0, n
    assert out == t
    assert "validate_path" in _undef_of(out)


def test_4_mixed_partial_repair_still_escalates():
    t = _fence("def main(x):\n    return os.path.join(validate_path(x))")
    out, n = _repair_missing_imports(t)
    assert n == 1, n
    assert "import os" in out
    assert "validate_path" in _undef_of(out), "user helper must remain undefined -> escalate"


def test_5_already_correct_unchanged():
    t = _fence("import os\n\ndef main():\n    return os.getcwd()")
    out, n = _repair_missing_imports(t)
    assert n == 0, n
    assert out == t


def test_6_syntax_broken_unchanged():
    t = _fence("def main(:\n    return os.getcwd()")
    out, n = _repair_missing_imports(t)
    assert n == 0, n
    assert out == t
