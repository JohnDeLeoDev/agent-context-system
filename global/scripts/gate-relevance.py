#!/usr/bin/env python3
"gate-relevance.py — can this branch's changes affect a build at all?\n\nTHE LIST IS DELIBERATELY TINY: a path is irrelevant here only if it cannot\naffect build output on ANY stack in the fleet. Per-repo additions (passed\nafter the refs) cover patterns safe for one stack but not fleet-wide, such\nas `*.md` on a repo with no docs build.\n\nUsage:  python3 .../gate-relevance.py <repo> <base-ref> <branch-ref> [extra-glob ...]\n  exit 0  every changed path is build-irrelevant — the gate may be skipped\n  exit 1  something could affect a build — run the gate\n  exit 2  the question could not be answered (bad refs, not a repo) — run the gate"

import fnmatch
import subprocess
import sys

FLEET_IRRELEVANT_NAMES = {
    ".gitignore", ".gitattributes", ".mailmap", ".editorconfig",
    "LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "CODEOWNERS",
    ".gitkeep",
}

FLEET_IRRELEVANT_GLOBS = (
    "*/.gitignore", "*/.gitattributes", "*/.editorconfig", "*/.gitkeep",
)


def is_irrelevant(path, extra_globs):
    if path in FLEET_IRRELEVANT_NAMES:
        return True
    for glob in FLEET_IRRELEVANT_GLOBS:
        if fnmatch.fnmatchcase(path, glob):
            return True
    for glob in extra_globs:
        if fnmatch.fnmatchcase(path, glob):
            return True
    return False


def main(argv):
    repo = argv[0] if len(argv) >= 1 else ""
    base = argv[1] if len(argv) >= 2 else ""
    branch = argv[2] if len(argv) >= 3 else ""

    if not repo or not base or not branch:
        print("gate-relevance: need <repo> <base-ref> <branch-ref> [extra-glob ...]",
              file=sys.stderr)
        return 2
    extra_globs = argv[3:]

    proc = subprocess.run(["git", "-C", repo, "diff", "--name-only",
                           "%s...%s" % (base, branch)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        print("gate-relevance: could not diff %s...%s in %s — run the gate"
              % (base, branch, repo), file=sys.stderr)
        return 2
    changed = proc.stdout.rstrip("\n")

    
    
    
    if not changed:
        print("gate-relevance: %s...%s reports no changed files — run the gate"
              % (base, branch), file=sys.stderr)
        return 2

    paths = [line for line in changed.split("\n") if line]
    relevant = [p for p in paths if not is_irrelevant(p, extra_globs)]

    if relevant:
        return 1

    print("gate-relevance: all %d changed path(s) are build-irrelevant (%s)"
          % (len(paths), " ".join(paths)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
