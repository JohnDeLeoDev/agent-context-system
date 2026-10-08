#!/usr/bin/env python3
'change-size-check -- is this change small enough to actually be reviewed?\n\nWHY THIS EXISTS. Review effectiveness -- defects caught per review -- is not a smooth\nfunction of size, it falls off a cliff. Measured repeatedly across large engineering\norgs: 80-90% of defects caught at ~200 changed lines, below 70% past 400, below 50%\nat 1,000. Past the threshold reviewers spend less time PER LINE, leave fewer\nsubstantive comments, and are likelier to approve without requesting changes. Tangled\nchanges -- several unrelated things in one diff -- inflate defect-prediction error by\n5-200%, and decomposed changesets measurably reduce wrongly-reported issues.\n\nThat matters more with an agent than without one. An agent can produce 900 lines in\nthe time it takes to read 90, so the natural size of a change has gone up while the\nsize a human can actually review has not moved at all. Nothing in this fleet bounded\nit: the worktree mandate structures WHERE a change happens and says nothing about HOW\nBIG it gets before it lands.\n\nWARNS, NEVER BLOCKS, and that is deliberate. A big change is sometimes right -- a\nmechanical rename, a generated file, a vendored import -- and a gate that refuses\nthose gets an override pasted in front of it permanently, which is how a guard stops\nguarding anything. The point is to make the size VISIBLE at the moment someone\ndecides to land it, because the failure is not "a big diff exists", it is "a big diff\nwas reviewed as if it were a small one".\n\nUsage:\n  change-size-check.py [<base-ref>] [<repo>]    # default base: main, repo: $PWD\n  change-size-check.py --quiet ...              # print only when over threshold'
import os
import subprocess
import sys

GOOD = 200      
CLIFF = 400     
DASH = "—"


def git_ok(repo, *args):
    proc = subprocess.run(["git", "-C", repo] + list(args),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.returncode == 0


def main(argv):
    quiet = False
    args = []
    for a in argv:
        if a == "--quiet":
            quiet = True
        else:
            args.append(a)

    base = args[0] if len(args) > 0 else ""
    repo = args[1] if len(args) > 1 else os.getcwd()

    if not git_ok(repo, "rev-parse", "--git-dir"):
        return 0

    
    
    
    if not base:
        for cand in ("main", "master", "develop"):
            if git_ok(repo, "rev-parse", "--verify", "--quiet", cand):
                base = cand
                break
    if not base:
        return 0
    if not git_ok(repo, "rev-parse", "--verify", "--quiet", base):
        return 0

    
    
    proc = subprocess.run(["git", "-C", repo, "merge-base", "HEAD", base],
                          capture_output=True, text=True)
    mb = proc.stdout.strip() if proc.returncode == 0 else base

    proc = subprocess.run(["git", "-C", repo, "diff", "--numstat", "%s...HEAD" % mb],
                          capture_output=True, text=True)
    added = removed = 0
    for line in (proc.stdout or "").splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        try:
            added += int(fields[0])
        except ValueError:
            pass
        try:
            removed += int(fields[1])
        except ValueError:
            pass

    proc = subprocess.run(["git", "-C", repo, "diff", "--name-only", "%s...HEAD" % mb],
                          capture_output=True, text=True)
    files = sum(1 for line in (proc.stdout or "").splitlines() if line)
    total = added + removed

    if total <= 0:
        return 0

    if total <= GOOD:
        if not quiet:
            print("change-size: %d changed line(s) across %d file(s) %s inside the "
                  "~%d-line band where review catches most defects." % (total, files, DASH, GOOD))
        return 0

    if total <= CLIFF:
        print("change-size: %d changed line(s) across %d file(s) %s above the "
              "~%d-line band." % (total, files, DASH, GOOD), file=sys.stderr)
        print("             Still reviewable, but defect-catch rate is already falling. "
              "Consider landing in slices.", file=sys.stderr)
        return 0

    
    
    print("change-size: ⚠ %d changed line(s) across %d file(s) %s past the "
          "~%d-line cliff." % (total, files, DASH, CLIFF), file=sys.stderr)
    print("             Review effectiveness drops below 70% here, below 50% near "
          "1,000 lines.", file=sys.stderr)
    print("             Land it as stacked slices, each doing one thing, or say why "
          "it's atomic (rename, generated output, vendored import).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
