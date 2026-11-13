"""Pin dirtycheck's .dockerignore matching, especially in the FAIL-CLOSED direction.

The whole value of dirtycheck.py is that it may only call a file "excluded" when that is
provably true. A false-clean stamps an image with a commit it does not match -- the exact
failure (bifrost-kb / live-v214) the dirty-tree guard exists to catch. So the cases that assert
`excluded is False` are the important ones here; the True cases are merely the feature.
"""
import importlib.util
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "dirtycheck.py")

_spec = importlib.util.spec_from_file_location("dirtycheck", _SRC)
dirtycheck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dirtycheck)

# A faithful subset of the router's real .dockerignore, in file order. Order matters: the
# skills/ re-includes appear AFTER the broad exclusions, exactly as they do in the real file.
PATS = [
    (False, "__pycache__/"), (False, "*.pyc"), (False, ".env"), (False, "models/"),
    (False, "skills/"), (False, "*.log"), (False, ".git/"), (False, "tests/"),
    (False, "*.md"), (False, "patch-*.py"),
    (True, "skills/"), (True, "skills/**"),
    (False, "bench_*.py"), (False, "analysis_*.py"),
    (False, "*.bak"), (False, "*.bak-*"), (False, ".claude/"), (False, ".github/"),
    (False, "k8s/"), (False, "scratch/"), (False, "*.jsx"), (False, "Dockerfile*"),
    (False, "*.yaml"), (False, "*.ps1"), (False, "test_*.py"), (False, "fix_*.py"),
    (True, "skills/"), (True, "skills/**"),
    (False, "scratchpad/"),
    (False, "bifrost_telegram.py"), (False, "format_stress_guard.py"), (False, "tools/warmup.py"),
]


@pytest.mark.parametrize("path,expected,why", [
    # --- MUST be dirty. A regression here silently un-verifies a production image. ---
    ("main.py", False, "real source at the root"),
    ("config.py", False, "protected source"),
    ("backends/ollama.py", False, "real source in a package dir"),
    ("tools/other.py", False, "sibling of an excluded file is NOT itself excluded"),
    ("skills/registry.json", False, "skills/ is re-included by a later ! pattern"),
    ("skills/deep/nested.md", False, "skills/** re-include beats the broad *.md exclusion"),
    ("requirements.txt", False, "no pattern matches it at all"),
    # --- provably excluded: cannot reach the image, so must not make a build -dirty ---
    (".github/workflows/ai-review.yml.proposed", True, "the 2026-09-04 case"),
    (".github/workflows/ai-review.yml.optionD.patch", True, "the 2026-09-04 case"),
    ("tests/test_x.py", True, "tests/ is not shipped"),
    ("scratch/probe.py", True, "dev scratch"),
    ("bench_foo.py", True, "bench_*.py"),
    ("NOTES.md", True, "*.md"),
    ("k8s/deploy.yaml", True, "k8s/"),
    ("build.ps1", True, "*.ps1"),
    ("tools/warmup.py", True, "excluded by the 2026-09-03 provenance block"),
])
def test_excluded_from_image(path, expected, why):
    assert dirtycheck.excluded_from_image(path, PATS) is expected, why


def test_match_error_is_counted_as_reaching_the_image(monkeypatch, capsys):
    """Fail-closed: if the matcher raises, main() must report DIRTY, not swallow the file.

    This is the guarantee that matters. A first attempt at this test asserted that path_match
    itself raises on a hostile pattern -- it does not, because `pattern.strip()` returns a plain
    str before any custom split is reached, so the test passed on a mechanism that never fires.
    Exercise the real contract instead: an exception anywhere in the per-file decision must
    surface as dirty.
    """
    monkeypatch.setattr(dirtycheck, "load_dockerignore", lambda: [(False, "*.md")])
    monkeypatch.setattr(dirtycheck, "git", lambda *a: [] if "--untracked-files=no" in a
                        else ["some_new_file.py"])

    def boom(path, pats):
        raise ValueError("matcher exploded")
    monkeypatch.setattr(dirtycheck, "excluded_from_image", boom)

    assert dirtycheck.main() == 1
    out = capsys.readouterr().out
    assert "DIRTY" in out
    assert "some_new_file.py" in out
    assert "match error" in out


def test_git_failure_is_undecided_not_clean(monkeypatch, capsys):
    """A check that cannot run is not a pass -- exit 2, and callers must treat it as dirty."""
    def boom(*a):
        raise RuntimeError("git exploded")
    monkeypatch.setattr(dirtycheck, "git", boom)
    assert dirtycheck.main() == 2
    out = capsys.readouterr().out
    assert "UNDECIDED" in out
    assert "CLEAN" not in out


def test_star_does_not_cross_a_slash():
    """`*` is a single-segment wildcard, as in Go's filepath.Match -- not a recursive one."""
    assert dirtycheck.path_match("*.py", "main.py") is True
    assert dirtycheck.path_match("*.py", "backends/ollama.py") is False
    assert dirtycheck.path_match("**/*.py", "backends/ollama.py") is True


def test_status_path_survives_the_strip_in_git():
    """git() strips each line, so the porcelain status prefix is NOT at a fixed offset.

    Reading it as line[3:] ate into the filename. These are the shapes git() actually yields.
    """
    assert dirtycheck._status_path("M evals/x.jsonl") == "evals/x.jsonl"
    assert dirtycheck._status_path("MM a/b.py") == "a/b.py"
    assert dirtycheck._status_path("?? untracked.txt") == "untracked.txt"
    assert dirtycheck._status_path("D gone.py") == "gone.py"
    # A rename must yield the NEW path -- that is the one that would land in the image.
    assert dirtycheck._status_path("R old.py -> new.py") == "new.py"
    assert dirtycheck._status_path("") is None
    assert dirtycheck._status_path("M") is None


def test_tracked_change_to_a_dockerignored_file_is_CLEAN(monkeypatch, capsys):
    """The defect this whole check exists to kill, arriving through the tracked branch.

    A plain test run modifies evals/execution_traces.jsonl. `evals/` is dockerignored, so that
    file cannot reach the image and must not stamp the build -dirty.
    """
    monkeypatch.setattr(dirtycheck, "load_dockerignore", lambda: [(False, "evals/")])
    monkeypatch.setattr(dirtycheck, "git", lambda *a:
                        ["M evals/execution_traces.jsonl"]
                        if "--untracked-files=no" in a else [])
    rc = dirtycheck.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert out.startswith("CLEAN"), out
    assert "PROVABLY excluded" in out, out


def test_tracked_change_to_a_REAL_source_file_is_DIRTY(monkeypatch, capsys):
    """Negative control. The fix above must not turn the checker into one that never fires.

    Without this, a change that made excluded_from_image() return True for everything would
    satisfy the test above and silence the guard completely.
    """
    monkeypatch.setattr(dirtycheck, "load_dockerignore", lambda: [(False, "evals/")])
    monkeypatch.setattr(dirtycheck, "git", lambda *a:
                        ["M main.py"] if "--untracked-files=no" in a else [])
    rc = dirtycheck.main()
    out = capsys.readouterr().out
    assert rc == 1, out
    assert out.startswith("DIRTY"), out
    assert "main.py" in out, out


def test_unparseable_tracked_line_counts_as_reaching(monkeypatch, capsys):
    """Fail closed: a path we cannot read is assumed to reach the image, never assumed safe."""
    monkeypatch.setattr(dirtycheck, "load_dockerignore", lambda: [(False, "evals/")])
    monkeypatch.setattr(dirtycheck, "git", lambda *a:
                        ["M"] if "--untracked-files=no" in a else [])
    rc = dirtycheck.main()
    out = capsys.readouterr().out
    assert rc == 1, out
    assert out.startswith("DIRTY"), out
