"""A subtask id is a label, not a module -- the L5 payload must not `import s1`.

WHY. dep_context is headed "### Output of subtask s1", and models read that as a module
name. The dep-inclusive merge (2026-07-23) inlines that dependency INTO the same module,
so `import s1` resolves to nothing and the merged program raises ModuleNotFoundError at
import -- with the implementation sitting in the same file, three lines up.

This was invisible until the sandbox stopped failing first. Live timeline 2026-07-23:
seven consecutive s4 verdicts reading "No module named 'pytest'", then execsvc v3 (the
stdlib pytest shim) went live at 21:31 UTC, and the very next s4 verdict at 21:37 read
"No module named 's1'". One masked defect uncovering the next.

The rewrite is bounded by the leg's DECLARED dependency ids, so it cannot touch a real
third-party import.
"""
import pytest

from subtask_graph import _neutralise_dep_id_imports


def test_plain_import_becomes_self_alias():
    code = "import s1\n\ndef test_main():\n    assert s1.main([]) == 0\n"
    out, n = _neutralise_dep_id_imports(code, ["s1"])
    assert n == 1
    assert "import s1\n" not in out
    assert "s1 = _bifrost_self" in out
    assert "_bifrost_self = _bifrost_sys.modules[__name__]" in out
    # The call site is untouched -- that is the point of aliasing instead of deleting.
    assert "s1.main([])" in out


def test_from_import_is_dropped():
    code = "from s1 import main, load_csv\n\ndef test_x():\n    assert main([]) == 0\n"
    out, n = _neutralise_dep_id_imports(code, ["s1"])
    assert n == 1
    assert "from s1 import" not in out
    # Names come from the inlined dep code, so the bare call must survive.
    assert "assert main([]) == 0" in out


def test_import_as_alias():
    code = "import s1 as impl\n\ndef test_x():\n    assert impl.main([]) == 0\n"
    out, n = _neutralise_dep_id_imports(code, ["s1"])
    assert n == 1
    assert "impl = _bifrost_self" in out
    assert "impl.main([])" in out


def test_multiple_dep_ids():
    code = "import s1\nfrom s3 import validate_path\n\ndef test_x():\n    assert s1.main([]) == 0\n"
    out, n = _neutralise_dep_id_imports(code, ["s1", "s3"])
    assert n == 2
    assert "s1 = _bifrost_self" in out
    assert "from s3 import" not in out


@pytest.mark.parametrize("real", [
    "import pytest\n",
    "import os\nimport sys\n",
    "from statistics import mean, median\n",
    "from csv import reader\n",
])
def test_real_imports_are_never_touched(real):
    out, n = _neutralise_dep_id_imports(real, ["s1"])
    assert n == 0
    assert out == real


def test_undeclared_id_is_left_alone():
    """Bounded by DECLARED deps: an id this leg never depended on is not ours to rewrite."""
    code = "import s7\n"
    out, n = _neutralise_dep_id_imports(code, ["s1"])
    assert n == 0
    assert out == code


def test_no_prelude_when_nothing_rewritten():
    code = "import pytest\n\ndef test_x():\n    assert True\n"
    out, n = _neutralise_dep_id_imports(code, ["s1"])
    assert n == 0
    assert "_bifrost_sys" not in out


def test_output_still_compiles():
    code = "import s1\n\ndef test_main():\n    assert s1.main([]) == 0\n"
    out, _ = _neutralise_dep_id_imports(code, ["s1"])
    compile(out, "<merged>", "exec")


# --- implementation-name imports (added 2026-07-25) -------------------------------------
# A subtask id is not the only way a test leg reaches for the inlined impl. Live proof
# (graph 66d69686, v251): a test leg that HAD dep_context still wrote
#   from main import main  # Assuming the implementation is in a file named main.py
# -> ModuleNotFoundError. `main` is the impl's function name, not a subtask id, so the
# sN-bounded rule missed it. The impl code is passed as a 3rd arg; imports of names the dep
# DEFINES are neutralized, while real third-party imports (names the dep does not define)
# are left alone.
_IMPL = (
    "import argparse\n\n"
    "def main(argv=None):\n    return 1\n\n"
    "def is_numeric(v):\n    return v.isdigit()\n"
)


def test_proven_from_impl_name_import_is_dropped():
    """The exact live failure: `from main import main` where main is defined by the dep."""
    code = ("from main import main  # Assuming the implementation is in a file named main.py\n"
            "\ndef test_x():\n    assert main() == 1\n")
    out, n = _neutralise_dep_id_imports(code, ["s1"], _IMPL)
    assert n == 1
    assert "from main import" not in out
    assert "assert main() == 1" in out
    compile(out, "<merged>", "exec")


def test_guessed_module_name_import_is_dropped():
    """Module guessed from the tool name, importing a name the dep defines -> drop."""
    code = "from csv_tool import main, is_numeric\n\ndef test_y():\n    assert main() == 1\n"
    out, n = _neutralise_dep_id_imports(code, ["s1"], _IMPL)
    assert n == 1
    assert "from csv_tool import" not in out


def test_bare_import_of_impl_name_is_dropped():
    code = "import is_numeric\n\ndef test_z():\n    assert is_numeric('3')\n"
    out, n = _neutralise_dep_id_imports(code, ["s1"], _IMPL)
    assert n == 1
    assert "import is_numeric\n" not in out


@pytest.mark.parametrize("real", [
    "import tempfile\n",
    "import os, io\n",
    "from io import StringIO\n",
    "from contextlib import redirect_stdout\n",
])
def test_impl_names_do_not_touch_real_imports(real):
    code = real + "\ndef test_a():\n    assert main() == 1\n"
    out, n = _neutralise_dep_id_imports(code, ["s1"], _IMPL)
    assert real in out


def test_partial_impl_names_from_import_survives():
    """A `from pkg import a, b` where only SOME names are impl-defined must NOT be dropped."""
    code = "from mypkg import main, not_a_dep_name\n\ndef test_b():\n    pass\n"
    out, n = _neutralise_dep_id_imports(code, ["s1"], _IMPL)
    assert n == 0
    assert "from mypkg import main, not_a_dep_name" in out


def test_no_impl_code_preserves_legacy_behaviour():
    """Called with the old 2-arg signature, impl-name handling is inert."""
    code = "from main import main\n\ndef test_c():\n    assert main() == 1\n"
    out, n = _neutralise_dep_id_imports(code, ["s1"])
    assert n == 0
    assert out == code
