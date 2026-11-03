"""Extract each `run:` block of a workflow into a standalone shell script.

Used by tools/ci/arms.sh so that an arm can run ONE CI step against a mutated tree without
a GitHub runner. The scripts it writes are named
    <job>__<index>__<slugged-step-name>.sh
so an arm can address a step by job and position without hard-coding a line number.

NO SHEBANG, deliberately: this is invoked as `python3 tools/ci/extract-steps.py`, never
directly, so the step-4 preflight's A5 obligation must not require it to be 100755. A file
that carries a shebang and is not executable in the index is the defect A5 exists to catch;
a library that carries neither is simply not a script.

A step's `env:` block is emitted as `export` lines, EXCEPT where the value is a ${{ }}
expression -- those cannot be evaluated outside a runner, so they are emitted as a comment
and the caller is responsible for supplying them. Silently emitting an empty value instead
would make a step take a branch the real runner never takes, which is the whole class of
defect this repository has been chasing.
"""

import os
import re
import sys

import yaml


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def main():
    workflow, outdir = sys.argv[1], sys.argv[2]
    with open(workflow) as fh:
        doc = yaml.safe_load(fh) or {}
    os.makedirs(outdir, exist_ok=True)
    written = []
    for job_name, job in (doc.get("jobs") or {}).items():
        for index, step in enumerate(job.get("steps") or []):
            if "run" not in step:
                continue
            path = os.path.join(
                outdir, "%s__%02d__%s.sh" % (job_name, index, slug(step.get("name", "step")))
            )
            with open(path, "w") as fh:
                fh.write("#!/usr/bin/env bash\n")
                for key, value in (step.get("env") or {}).items():
                    value = str(value)
                    if "${{" in value:
                        fh.write("# env %s is a ${{ }} expression; the caller must export it\n" % key)
                    else:
                        fh.write("export %s=%s\n" % (key, "'" + value.replace("'", "'\\''") + "'"))
                fh.write(step["run"])
            written.append(path)
    if not written:
        sys.stderr.write("extract-steps: %s yielded NO run blocks\n" % workflow)
        return 1
    print("\n".join(written))
    return 0


if __name__ == "__main__":
    sys.exit(main())
