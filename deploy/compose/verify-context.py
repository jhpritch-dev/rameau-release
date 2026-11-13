import os, sys, importlib.util
sys.stdout.reconfigure(encoding="ascii", errors="replace")

REPO = r"F:\RAMEAU_STAGE\2026-10-05\bifrost-router"
NEW  = r"F:\RAMEAU_STAGE\2026-10-05\_build\router\.dockerignore"

# load the repo's OWN matcher -- the one build.ps1 trusts to decide the -dirty stamp
spec = importlib.util.spec_from_file_location("dc", os.path.join(REPO, "dirtycheck.py"))
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)
print("loaded dirtycheck.py; has path_match=%s excluded_from_image=%s"
      % (hasattr(dc, "path_match"), hasattr(dc, "excluded_from_image")))

def load(p):
    pats = []
    for line in open(p, "r", encoding="utf-8-sig").read().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        pats.append((neg, line[1:].strip() if neg else line))
    return pats

for label, patfile in (("PRE-EXISTING", os.path.join(REPO, ".dockerignore")), ("NEW (deny-all floor)", NEW)):
    pats = load(patfile)
    incl, excl = [], []
    for dp, dns, fns in os.walk(REPO):
        dns[:] = [d for d in dns if d not in (".git", "__pycache__")]
        for f in fns:
            rel = os.path.relpath(os.path.join(dp, f), REPO).replace("\\", "/")
            if dc.excluded_from_image(rel, pats):
                excl.append(rel)
            else:
                incl.append(rel)
    tot = sum(os.path.getsize(os.path.join(REPO, p.replace("/", os.sep))) for p in incl)
    print()
    print("### %s" % label)
    print("    REACHES the image: %d files, %d bytes" % (len(incl), tot))
    print("    excluded:          %d files" % len(excl))
    if "NEW" in label:
        print("    --- the full include set ---")
        for p in sorted(incl):
            print("      %s" % p)

# the load-bearing assertions
pats = load(NEW)
MUST_IN = ["main.py", "bifrost_delegate.py", "backends/ollama.py", "fleet_config.json",
           "capability_registry.json", "capability_profiles.json", "bifrost_shell_policy.json",
           "requirements.txt", "skills/router/SKILL.md", "subtask_graph.py"]
MUST_OUT = ["bfg-1.14.0.jar", "BifrostPortal.jsx", "tests/test_undefined_names.py",
            "k8s/bifrost-router-deployment.yaml", "Dockerfile.v50", "router.log",
            "backends/block2_4_opener.py", "bifrost_shell.py", "executors.json",
            "scratchpad/demo_bank.py", "dispatch-journal/DISPATCH_INDEX.md",
            "autopilot_graph.py.pre-training", "main.py.v72_backup"]
print()
print("### ASSERTIONS (via dirtycheck.excluded_from_image)")
bad = 0
for p in MUST_IN:
    ok = not dc.excluded_from_image(p, pats)
    print("    %-45s MUST REACH  : %s" % (p, "ok" if ok else "*** FAIL ***"))
    bad += 0 if ok else 1
for p in MUST_OUT:
    ok = dc.excluded_from_image(p, pats)
    print("    %-45s MUST NOT    : %s" % (p, "ok" if ok else "*** FAIL ***"))
    bad += 0 if ok else 1
print("    failures: %d" % bad)
