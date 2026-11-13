"""Every local module bifrost_shell.py imports must be COPYed into the image.

WHY THIS EXISTS. Ceremony 3 added `import bifrost_gates` and Ceremony 5 added
`import bifrost_halt`, both inside a guarded try that degrades to "no gates" / "no
halt switch" on ImportError. That guard is correct -- a missing file must not take the
tool surface down. But it means a file left out of the Dockerfile produces an image
where the security control is SILENTLY INERT: every tool still works, the surface is
byte-identical, and the only signal is one _audit.warning line in a container log
nobody reads.

Both files were in fact missing from the COPY line, discovered 2026-09-21. The gate
and the kill switch were verified working against the repo on disk and would NOT have
existed in the deployed image. A guard that cannot fire is worse than no guard,
because it is believed.

So the property is checked here, in the suite, where it fails loudly and early --
rather than being rediscovered the next time someone needs the kill switch to work.
"""
import ast
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERFILE = "Dockerfile.bifrost-shell-mcp-stdio-v3"
ENTRYPOINT = "bifrost_shell.py"

#: Data files read relative to the module at runtime. These have no import statement,
#: so nothing else in this test would notice them going missing.
RUNTIME_DATA = ["bifrost_shell_policy.json", "bifrost-gates.toml"]


def _logical_lines(path):
    """Dockerfile lines with backslash continuations joined, comments dropped.

    The first version of this read the file line by line. The moment the COPY line was
    split across two lines with a trailing backslash -- which is the normal way to
    write a long COPY -- it saw only the first half and reported the second half's
    files as missing. It failed safe, but it failed for the wrong reason, and a guard
    that cries wolf at correct input gets disabled by the next person who hits it.
    """
    out, buf = [], ""
    for raw in open(path, encoding="utf-8", errors="replace"):
        line = raw.split("#", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            if buf:
                continue
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        out.append((buf + line).strip())
        buf = ""
    if buf:
        out.append(buf.strip())
    return out


def _copied_files():
    path = os.path.join(REPO, DOCKERFILE)
    assert os.path.exists(path), \
        "%s not found -- this guard must not silently skip" % DOCKERFILE
    names = set()
    for s in _logical_lines(path):
        if not s.upper().startswith("COPY "):
            continue
        parts = s.split()[1:]
        if parts and parts[-1] in ("./", ".", "/app/", "/app"):
            parts = parts[:-1]
        for tok in parts:
            if not tok.startswith("--"):
                names.add(os.path.basename(tok))
    assert names, "parsed no COPY targets out of %s" % DOCKERFILE
    return names


def test_the_parser_handles_a_continued_copy_line(tmp_path):
    """Pins the bug above: a split COPY must be read as one instruction."""
    p = tmp_path / "Dockerfile.probe"
    p.write_text("FROM x\n# a comment\nCOPY a.py b.py \\\n     c.py d.toml ./\n",
                 encoding="utf-8")
    lines = _logical_lines(str(p))
    joined = [l for l in lines if l.upper().startswith("COPY ")]
    assert len(joined) == 1, "continuation was not joined: %r" % lines
    for want in ("a.py", "b.py", "c.py", "d.toml"):
        assert want in joined[0], "%s lost when joining: %r" % (want, joined[0])


def _local_imports_of(filename):
    """Top-level module names imported by `filename` that are .py files in this repo."""
    src = open(os.path.join(REPO, filename), encoding="utf-8", errors="replace").read()
    tree = ast.parse(src)
    found = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            found.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            found.add(n.module.split(".")[0])
    return {m for m in found if os.path.exists(os.path.join(REPO, m + ".py"))}


def test_the_dockerfile_copies_every_local_module_the_entrypoint_imports():
    copied = _copied_files()
    needed = _local_imports_of(ENTRYPOINT)
    assert needed, "parsed no local imports from %s -- the scan did not run" % ENTRYPOINT

    missing = sorted(m + ".py" for m in needed if m + ".py" not in copied)
    assert not missing, (
        "%s imports %s but %s does not COPY them. The imports are inside a guarded "
        "try, so the image would build, run, and serve every tool with the control "
        "silently disabled." % (ENTRYPOINT, missing, DOCKERFILE))


def test_the_dockerfile_copies_the_runtime_data_files():
    """These are read via Path(__file__).parent, so no import statement names them and
    the test above cannot see them."""
    copied = _copied_files()
    missing = [f for f in RUNTIME_DATA if f not in copied]
    assert not missing, (
        "%s does not COPY %s. These are read at runtime relative to the module; "
        "without them the gates load fails and _gate_denies fails open."
        % (DOCKERFILE, missing))


def test_transitive_local_imports_are_copied_too():
    """bifrost_gates imports nothing local today, but if it ever does, that module has
    to ship as well -- protection follows the call graph and so does packaging."""
    copied = _copied_files()
    seen, queue, missing = set(), [ENTRYPOINT], []
    while queue:
        cur = queue.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for mod in _local_imports_of(cur):
            fn = mod + ".py"
            if fn not in copied:
                missing.append("%s (imported by %s)" % (fn, cur))
            if fn not in seen:
                queue.append(fn)
    assert not missing, "not COPYed into the image: %s" % sorted(set(missing))


def test_the_guarded_imports_are_still_guarded():
    """The COPY fix must not tempt anyone to drop the try. Both properties are needed:
    shipped AND guarded. Shipped-but-unguarded turns a packaging slip into an outage."""
    src = open(os.path.join(REPO, ENTRYPOINT), encoding="utf-8",
               errors="replace").read()
    tree = ast.parse(src)

    def imported_in(node, name):
        return any(isinstance(n, ast.Import) and any(a.name == name for a in n.names)
                   for n in ast.walk(node))

    for mod in ("bifrost_gates", "bifrost_halt"):
        if not imported_in(tree, mod):
            pytest.skip("%s is not imported by %s" % (mod, ENTRYPOINT))
        guarded = any(imported_in(n, mod) for n in ast.walk(tree)
                      if isinstance(n, ast.Try))
        assert guarded, "%s is imported outside a try in %s" % (mod, ENTRYPOINT)
