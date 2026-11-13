"""The kill switch, and the two fail-open defects it was built to close.

Every test here is about the switch behaving correctly when something is ALREADY
wrong -- that is the only situation in which anyone reaches for it.
"""
import os

import pytest

import bifrost_halt as H


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(H.HALT_ENV, raising=False)
    monkeypatch.delenv(H.HALT_FILE_ENV, raising=False)
    yield


# ---------------------------------------------------------------- arming

def test_unarmed_by_default(tmp_path):
    assert H.halted(str(tmp_path)) is False
    assert H.reason(str(tmp_path)) == ""


def test_the_env_var_arms_it(monkeypatch, tmp_path):
    monkeypatch.setenv(H.HALT_ENV, "1")
    assert H.halted(str(tmp_path)) is True
    assert H.HALT_ENV in H.reason(str(tmp_path))


def test_the_sentinel_file_arms_it(tmp_path):
    (tmp_path / H.DEFAULT_HALT_FILENAME).write_text("", encoding="utf-8")
    assert H.halted(str(tmp_path)) is True
    assert "sentinel file" in H.reason(str(tmp_path))


def test_an_empty_sentinel_file_still_arms_it(tmp_path):
    """Existence is the signal. Requiring content would be syntax, and syntax is the
    thing this switch exists to avoid."""
    (tmp_path / H.DEFAULT_HALT_FILENAME).write_text("", encoding="utf-8")
    assert H.halted(str(tmp_path)) is True


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "halt",
                                   "yes please", " 1 ", "x"])
def test_anything_that_is_not_an_explicit_no_arms_it(monkeypatch, tmp_path, value):
    """Inverted truthiness, on purpose. A switch whose job is to STOP things must not
    be talked out of it by an unexpected value."""
    monkeypatch.setenv(H.HALT_ENV, value)
    assert H.halted(str(tmp_path)) is True, "%r failed to arm the switch" % value


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "FALSE", " off "])
def test_only_an_explicit_negative_stands_it_down(monkeypatch, tmp_path, value):
    monkeypatch.setenv(H.HALT_ENV, value)
    assert H.halted(str(tmp_path)) is False, "%r wrongly armed the switch" % value


def test_either_form_alone_is_enough(monkeypatch, tmp_path):
    """Neither can be partially applied, and one working is sufficient."""
    monkeypatch.setenv(H.HALT_ENV, "0")
    (tmp_path / H.DEFAULT_HALT_FILENAME).write_text("", encoding="utf-8")
    assert H.halted(str(tmp_path)) is True, "file form did not arm on its own"
    assert H.HALT_ENV not in H.reason(str(tmp_path))


def test_the_reason_names_which_form_armed_it(monkeypatch, tmp_path):
    """Otherwise disarming is guesswork, and a half-disarm looks exactly like a switch
    that did not work."""
    monkeypatch.setenv(H.HALT_ENV, "1")
    (tmp_path / H.DEFAULT_HALT_FILENAME).write_text("", encoding="utf-8")
    r = H.reason(str(tmp_path))
    assert H.HALT_ENV in r and "sentinel file" in r


def test_the_file_location_is_overridable(monkeypatch, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    flag = other / "STOP-NOW"
    monkeypatch.setenv(H.HALT_FILE_ENV, str(flag))
    assert H.halted(str(tmp_path)) is False
    flag.write_text("", encoding="utf-8")
    assert H.halted(str(tmp_path)) is True


# ------------------------------------------------------- cannot fail open

def test_it_never_raises_even_on_a_hostile_path():
    """This is called from inside the tool gate. An exception here would be caught by
    the caller's handler and turned into 'not halted' -- a crash that silently disarms.

    The hostile value is passed as an ARGUMENT, deliberately not through
    monkeypatch.setenv. An earlier version of this test used a NUL byte in setenv; the
    NUL cannot enter the environment at all, so setenv itself raised -- and pytest's
    traceback for a failure inside monkeypatch renders `self = environ({...})`, which
    dumps every environment variable, API keys included, into the test log. A test
    that leaks secrets when it fails is a worse defect than the one it was checking.
    """
    for hostile in ("", "   ", "?" * 4096, "C:\\nul\\<>|:*?", ".." * 2000):
        assert H.halted(hostile) in (True, False)
        assert isinstance(H.reason(hostile), str)


def test_an_exploding_os_path_does_not_propagate(monkeypatch, tmp_path):
    def boom(_):
        raise RuntimeError("filesystem gone")

    monkeypatch.setattr(os.path, "exists", boom)
    assert H.halted(str(tmp_path)) is False


def test_the_env_form_survives_a_dead_filesystem(monkeypatch, tmp_path):
    """The reason there are two forms: a read-only or unreachable filesystem must not
    take the switch with it."""
    def boom(_):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(os.path, "exists", boom)
    monkeypatch.setenv(H.HALT_ENV, "1")
    assert H.halted(str(tmp_path)) is True


# -------------------------------------------------------------- no syntax

def test_the_module_parses_nothing():
    """The whole design claim. If this module ever grows a parser, it has acquired the
    failure mode it exists to avoid."""
    import ast

    tree = ast.parse(open(H.__file__, encoding="utf-8").read())
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            imported.add((n.module or "").split(".")[0])
    for parser in ("json", "tomllib", "tomli", "yaml", "configparser", "csv", "re",
                   "xml", "pickle", "bifrost_gates"):
        assert parser not in imported, (
            "bifrost_halt imports %s -- a kill switch must have no syntax to get wrong"
            % parser)


def test_it_reads_no_file_contents():
    """Existence only. Reading content would mean content can be wrong."""
    import ast

    tree = ast.parse(open(H.__file__, encoding="utf-8").read())
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            assert n.func.id != "open", "bifrost_halt opens a file"
