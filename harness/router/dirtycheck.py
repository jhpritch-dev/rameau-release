"""Decide whether the working tree is dirty IN A WAY THAT CAN REACH THE IMAGE.

Why this exists (2026-09-04). build.ps1 computed dirtiness as `git status --porcelain`, which
INCLUDES untracked files -- `git describe --dirty`, the usual idiom, does not. Measured that day:
the router tree's only dirt was two untracked files under `.github/workflows/`, staged artifacts
for an OPEN security decision, dropped 2026-09-03. `.github/` is on the `.dockerignore` list, so
those files cannot enter the image -- yet every build since stamped `-dirty`, and
`deployed_source_in_git` paged with "running image claims no verifiable commit".

The guard was not wrong: a `-dirty` stamp genuinely is unverifiable. The DEFINITION was wrong.
The question that matters is "does the image content correspond to a commit?", and a file that
`.dockerignore` excludes cannot change the image.

FAIL-CLOSED, deliberately. The two error directions are not symmetric:
  false-dirty  -> a noisy page. Costs attention.
  false-clean  -> an image stamped with a commit it does not match. That is precisely the
                  failure (bifrost-kb / live-v214) the dirty-tree guard was built for.
So anything not PROVABLY excluded counts as dirty: an unparseable pattern, a git command that
fails, a missing .dockerignore, an unexpected exception -- all report dirty.

Exit 0 = clean (nothing that reaches the image differs from HEAD).
Exit 1 = dirty; the reasons are printed.
Exit 2 = could not decide, which is NOT a pass -- callers must treat it as dirty.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def git(*args):
    p = subprocess.run(("git", "-C", HERE) + args, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), p.stderr.strip()[:200]))
    return [l.strip() for l in p.stdout.splitlines() if l.strip()]


def seg_match(pat, seg):
    """fnmatch on ONE path segment. '*' and '?' do not cross '/', matching Go's filepath.Match."""
    import fnmatch
    return fnmatch.fnmatchcase(seg, pat)


def path_match(pattern, path):
    """Docker .dockerignore matching for one pattern against one relative path.

    Segment-wise with '**' matching any number of segments, via the standard DP. Docker cleans
    patterns first, so 'dir/' and 'dir' are the same pattern.
    """
    ppats = [s for s in pattern.strip("/").split("/") if s]
    parts = [s for s in path.strip("/").split("/") if s]
    if not ppats:
        return False
    # dp[j] = pattern prefix of length j matches the parts prefix consumed so far
    dp = [False] * (len(ppats) + 1)
    dp[0] = True
    for j in range(1, len(ppats) + 1):
        dp[j] = dp[j - 1] and ppats[j - 1] == "**"
    for part in parts:
        ndp = [False] * (len(ppats) + 1)
        for j in range(1, len(ppats) + 1):
            p = ppats[j - 1]
            if p == "**":
                # consume this part (stay at j) or skip the ** entirely (dp[j-1])
                ndp[j] = ndp[j] or dp[j] or dp[j - 1]
            elif dp[j - 1] and seg_match(p, part):
                ndp[j] = True
        dp = ndp
    return dp[len(ppats)]


def load_dockerignore():
    p = os.path.join(HERE, ".dockerignore")
    if not os.path.exists(p):
        raise RuntimeError(".dockerignore not found -- cannot prove anything is excluded")
    pats = []
    with open(p, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            neg = line.startswith("!")
            if neg:
                line = line[1:].strip()
            if not line:
                continue
            pats.append((neg, line))
    return pats


def excluded_from_image(path, pats):
    """True only if the path is PROVABLY excluded from the build context.

    Conservative on purpose, and stricter than Docker's own last-match-wins: a path counts as
    excluded when some non-negated pattern matches it (or one of its ancestor directories) AND
    no `!` pattern matches any of them. Any re-include at all makes it dirty. That can only err
    toward reporting dirty, never toward reporting clean.
    """
    prefixes = []
    parts = path.split("/")
    for i in range(1, len(parts) + 1):
        prefixes.append("/".join(parts[:i]))
    hit = False
    for neg, pat in pats:
        for pre in prefixes:
            if path_match(pat, pre):
                if neg:
                    return False
                hit = True
                break
    return hit


def _status_path(line):
    """The path out of one `git status --porcelain` line, or None if it cannot be read.

    Format is `XY <path>`, or `XY <old> -> <new>` for a rename -- the NEW path is the one that
    would land in the image. Paths containing unusual bytes are quoted by git; the quotes are
    stripped but nothing else is unescaped, and None means the caller must fail closed.
    """
    if not line:
        return None
    # NOT line[3:]. git() strips every line, so the porcelain " M path" prefix is no longer at
    # a fixed offset -- slicing three chars off the stripped "M path" ate into the filename and
    # produced "als/execution_traces.jsonl", which then matched no .dockerignore pattern and was
    # reported as reaching the image. Split on the status token instead.
    parts = line.split(None, 1)
    if len(parts) < 2:
        return None
    rest = parts[1].strip()
    if " -> " in rest:
        rest = rest.split(" -> ", 1)[1].strip()
    if len(rest) >= 2 and rest[0] == '"' and rest[-1] == '"':
        rest = rest[1:-1]
        if "\\" in rest:
            return None  # escaped bytes we will not guess at -- fail closed
    return rest.replace("\\", "/") or None


def main():
    try:
        pats = load_dockerignore()
        tracked = git("status", "--porcelain", "--untracked-files=no")
        untracked = git("ls-files", "--others", "--exclude-standard")
    except Exception as e:
        print("UNDECIDED: %s" % e)
        print("Treat as DIRTY. This check fails closed by design.")
        return 2

    reasons = []

    # A tracked modification only matters if the file REACHES the image. Until 2026-09-04 this
    # branch skipped the .dockerignore check that the untracked branch has always done, and
    # asserted "these DO reach the image" about paths that provably cannot -- so a test run
    # touching evals/execution_traces.jsonl stamped the build -dirty, which is the same false
    # positive this checker was written to remove, arriving through the other half of the input.
    tracked_reaching, tracked_excluded = [], []
    for line in tracked:
        for entry in (line, ):
            path = _status_path(entry)
            try:
                if path is None or not excluded_from_image(path, pats):
                    tracked_reaching.append(entry)
                else:
                    tracked_excluded.append(entry)
            except Exception as e:  # fail closed: an unmatchable path counts as reaching
                tracked_reaching.append("%s   (match error: %s)" % (entry, e))

    if tracked_reaching:
        reasons.append("%d tracked file(s) modified/staged/deleted that REACH the image "
                       "via `COPY . .`:" % len(tracked_reaching))
        for l in tracked_reaching[:20]:
            reasons.append("    %s" % l)

    reaching = []
    for f in untracked:
        f = f.replace("\\", "/")
        try:
            if not excluded_from_image(f, pats):
                reaching.append(f)
        except Exception as e:
            reaching.append("%s   (match error, counted as reaching: %s)" % (f, e))
    if reaching:
        reasons.append("%d untracked file(s) NOT excluded by .dockerignore -- these reach the "
                       "image and match no commit:" % len(reaching))
        for f in reaching[:20]:
            reasons.append("    ?? %s" % f)

    ignored_ok = [f for f in untracked if f.replace("\\", "/") not in
                  [r.split("   ")[0] for r in reaching]]
    if reasons:
        print("DIRTY")
        for r in reasons:
            print(r)
        return 1

    print("CLEAN")
    if tracked_excluded:
        print("%d tracked file(s) modified but PROVABLY excluded by .dockerignore, so they "
              "cannot change the image:" % len(tracked_excluded))
        for l in tracked_excluded[:20]:
            print("    (ignored) %s" % l)
    if ignored_ok:
        print("%d untracked file(s) present but PROVABLY excluded by .dockerignore, so they "
              "cannot change the image:" % len(ignored_ok))
        for f in ignored_ok[:20]:
            print("    (ignored) %s" % f)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("UNDECIDED: unexpected %s: %s" % (type(e).__name__, e))
        print("Treat as DIRTY. This check fails closed by design.")
        sys.exit(2)
