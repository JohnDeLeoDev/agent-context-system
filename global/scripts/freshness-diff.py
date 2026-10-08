#!/usr/bin/env python3
"freshness-diff: which always-loaded entities describe a file that has moved on since?\n\nAn entity's `updated_at` is not tied to the mtime of what it governs, and the always tier\npays for that: its entities carry stale claims and dead pointers at a higher rate than\nthe others. The tier that describes the fastest-moving things -- a repo's\nlayout, a UI invariant, a daemon's flags -- is also the one nobody re-reads, because every\nsession is told about it and trusts it without re-deriving. This makes that specific\nstaleness visible: an entity edited before the last change to a file it names is a\ncandidate, not a verdict.\n\nWhy it is this narrow: do not widen it without measuring first. A general\npointer-resolution check over the whole corpus flags hundreds of pointers of which only\na handful are stale. A check that noisy gets disabled, and the global instruction says a\nnoisy check is fixed or deleted the same turn. So this one:\n\n  - reads always-loaded entities only,\n  - reports a path only when it resolves on this machine -- an unresolvable string is not\n    evidence of anything, it is usually a sibling repo or a file a loop creates,\n  - skips the four classes known to be noise (below),\n  - and reports, never edits.\n\nUsage:  freshness-diff.py [--json] [--all] [--days N]\n        --all    include lazy entities too (much noisier; for a deliberate sweep)\n        --days N only report a gap wider than N days (default 1)"
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store_task  

STORE = os.environ.get("AGENT_CONTEXT_STORE", os.path.expanduser("~/.agent-context"))
HOME = os.path.expanduser("~")


SKIP = (
    "/node_modules/",       
    "/.next/",              
    "/build/", "/dist/", "/target/", "/DerivedData/",
    "/__pycache__/",
    "/.git/",
)
SKIP_NAMES = {
    
    "OPEN.md", "SURFACES.md", "PROGRESS.md", "NOTES.md",
}



PATH_RE = re.compile(
    r"(?<![\w/])(~?/[\w.\-/]+\.\w{1,6}|(?:src|scripts|global|app|lib|hooks|server)/[\w.\-/]+\.\w{1,6})"
)


def entities(include_lazy):
    '(kind, key, updated_at_epoch, body, scope) for every store entity with front matter.'
    for root, dirs, files in os.walk(STORE):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "server")]
        for fn in files:
            if not fn.endswith(".md") or fn.endswith(".meta.toml"):
                continue
            p = os.path.join(root, fn)
            try:
                with open(p, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            if not text.startswith("---\n"):
                continue
            end = text.find("\n---\n", 4)
            if end == -1:
                continue
            fm, body = text[4:end], text[end + 5:]
            if not include_lazy and 'load_behavior: "always"' not in fm:
                continue
            m = re.search(r'updated_at:\s*"([^"]+)"', fm)
            if not m:
                continue
            try:
                ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                continue
            kind = (re.search(r'type:\s*"([^"]+)"', fm) or [None, "?"])[1]
            key = (re.search(r'(?:slug|path|name|title):\s*"([^"]+)"', fm) or [None, fn])[1]
            scope = (re.search(r'scope:\s*"([^"]+)"', fm) or [None, "global"])[1]
            yield kind, key, ts, body, scope, p


def candidate_paths(body, scope):
    'Resolvable, interesting filesystem paths named in `body`.'
    roots = [HOME]
    proj = scope.split(":", 1)[1] if ":" in scope else None
    if proj:
        roots += [os.path.join(HOME, "Developer", "example-workspace", proj),
                  os.path.join(HOME, "Developer", "Personal", proj)]
    seen = set()
    for raw in PATH_RE.findall(body):
        tok = raw[0] if isinstance(raw, tuple) else raw
        if any(s in tok for s in SKIP) or os.path.basename(tok) in SKIP_NAMES:
            continue
        cands = ([os.path.expanduser(tok)] if tok.startswith(("~/", "/"))
                 else [os.path.join(r, tok) for r in roots])
        for c in cands:
            if c in seen:
                continue
            seen.add(c)
            if os.path.isfile(c):
                yield tok, c
                break


def _usage_error(argv):
    '--help or an unrecognized flag: answered here, before any store decision, so\n    neither needs the daemon.'
    args = argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return 0
    known = {"--json", "--all", "--days"}
    unknown = [a for a in args if a not in known and not a.isdigit()]
    if unknown:
        print("freshness-diff: unknown flag(s): %s" % " ".join(unknown), file=sys.stderr)
        print(__doc__, file=sys.stderr)
        return 2
    return None


def run(argv):
    args = argv[1:]
    as_json = "--json" in args
    include_lazy = "--all" in args
    days = 1
    if "--days" in args:
        i = args.index("--days")
        if i + 1 < len(args) and args[i + 1].isdigit():
            days = int(args[i + 1])
    gap = days * 86400

    findings = []
    checked = 0
    for kind, key, ts, body, scope, _p in entities(include_lazy):
        checked += 1
        stale = []
        for tok, resolved in candidate_paths(body, scope):
            try:
                mt = os.path.getmtime(resolved)
            except OSError:
                continue
            if mt - ts > gap:
                stale.append({"pointer": tok, "resolved": resolved,
                              "file_newer_by_days": round((mt - ts) / 86400, 1)})
        if stale:
            stale.sort(key=lambda s: -s["file_newer_by_days"])
            findings.append({"kind": kind, "key": key, "scope": scope,
                             "entity_updated": time.strftime("%Y-%m-%d",
                                                             time.localtime(ts)),
                             "stale_pointers": stale[:5],
                             "worst_days": stale[0]["file_newer_by_days"]})

    findings.sort(key=lambda f: -f["worst_days"])

    if as_json:
        print(json.dumps({"checked": checked, "findings": findings}, indent=2))
        return 0

    tier = "always-loaded + lazy" if include_lazy else "always-loaded"
    if not findings:
        print("freshness-diff: %d %s entities, none describing a file newer than itself."
              % (checked, tier))
        return 0

    print("freshness-diff: %d of %d %s entities name a file changed since the entity was."
          % (len(findings), checked, tier))
    print("A candidate, not a verdict -- read the entity against the file before editing.\n")
    for f in findings:
        print("  %-9s %-46s (edited %s)" % (f["kind"], f["key"], f["entity_updated"]))
        for s in f["stale_pointers"]:
            print("      %-52s file is %.1fd newer" % (s["pointer"], s["file_newer_by_days"]))
    return 0


def main():
    return run(sys.argv)


if __name__ == "__main__":
    _early = _usage_error(sys.argv)
    if _early is not None:
        sys.exit(_early)
    store_task.main_or_forward("freshness-diff", main)
