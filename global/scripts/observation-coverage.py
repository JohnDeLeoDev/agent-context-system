#!/usr/bin/env python3
"observation-coverage — which paid-for lessons have no mechanism behind them?\n\nWhy this exists. The audit-observation corpus is this store's record of what has gone\nwrong. Most observations are one-off environmental faults with nothing to generalize.\nThe subset that came back is different: an observation with `recurrences >= 1` is a\nlesson that was learned, written down, resolved, and then repeated. If nothing\nmechanical cites it, the only thing standing between it and another sighting is that\nsomeone remembers. An audit done once by hand goes stale within a week; this report\nruns on every SessionStart.\n\nWhy not a block. Report-only, always exit 0. The backlog already exists, and a\ngate that refuses work until an unrelated backlog is cleared is a gate that gets skipped\npermanently -- the same reasoning store-precommit-gate gives for warning rather than\nblocking on invariants.\n\n  observation-coverage.py              the report\n  observation-coverage.py --all        include never-recurred high/blocker items\n  observation-coverage.py --json\n  observation-coverage.py --health     write a verdict for preflight\n\nObservations guarded: #410."
import glob
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp
import store_task  

STORE = os.environ.get("AGENT_CONTEXT_STORE", os.path.expanduser("~/.agent-context"))
OBS = os.path.join(STORE, "global", "audit-observations")




CORPUS = [
    os.path.join(STORE, "global", "hooks", "*"),
    os.path.join(STORE, "global", "scripts", "*"),
    os.path.join(STORE, "server", "src", "agent_context", "*.py"),
    os.path.join(STORE, "server", "tests", "*.py"),
    
    
    
    
    
    os.path.join(STORE, "global", "agents", "*.md"),
    
    
    
    os.path.join(STORE, "global", "instructions", "*.md"),
    os.path.join(STORE, "global", "memory", "*.md"),
]




EXEMPT = {}


def observations():
    out = []
    for path in glob.glob(os.path.join(OBS, "*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return out


def corpus_text():
    'Every file that could hold a mechanism -- except this one.\n\n    This script names observation numbers in its own docstring as examples, so scanning\n    itself would report those lessons as covered: the report would satisfy its own test\n    by describing the problem, and gaps would disappear with no sign. invariant-check\n    exempts itself by name for the same reason.'
    
    
    
    
    
    me = os.path.basename(os.path.abspath(__file__))
    blob, files = [], 0
    for pattern in CORPUS:
        for path in glob.glob(pattern):
            if not path.endswith((".py", ".sh", ".md")):
                continue
            if os.path.basename(path) == me:
                continue
            files += 1
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    blob.append(fh.read())
            except OSError:
                continue
    return "\n".join(blob), files


def cited(text, oid):
    'Does anything name this observation by number?\n\n    Anchored on the number with a word boundary so `#23` does not match `#230`, and\n    requiring an obs/observation/# lead-in so a bare year or count cannot satisfy it.'
    return bool(re.search(r"(?:obs(?:ervation)?s?\.?\s*#?|#)%s\b" % re.escape(oid),
                          text, re.I))


FILE_REF = re.compile(r"[\w./-]+\.(?:py|sh|md)\b")


def note_points_at_a_mechanism(r):
    'Does the resolution note name a file that exists, and is that file a mechanism?\n\n    A weaker signal than a citation, deliberately kept in its own tier rather than\n    folded into "covered". #230\'s note reads "...as server/tests/test_audit_id_collision\n    .py\'s docstring puts it", which is a real pointer to a real guard -- the test exists\n    and is exactly the mechanism -- but the test cites the duplicate id (#290), so a\n    by-number search calls it uncovered.\n\n    Not merged into the main test on purpose. Loosening the gap rule until nothing is\n    reported is how a coverage report becomes decorative; this store\'s rule is that a\n    check reporting a false positive gets fixed or deleted, and the mirror of it is that\n    a check reporting a false negative is worse, because it goes quiet. So these are\n    listed separately: visible, not counted as gaps, and a reader can judge each one.'
    note = " ".join(str(r.get(k) or "") for k in ("resolution_note", "notes"))
    for ref in FILE_REF.findall(note):
        ref = ref.strip("./")
        for base in (STORE, os.path.join(STORE, "global")):
            if os.path.exists(os.path.join(base, ref)):
                return ref
    return None


def split_tiers(gaps):
    '(hard, weak) — nothing at all, versus a note that points at a real file.\n\n    Split in one place so the report and the health verdict cannot disagree. If the\n    report listed hard gaps while --health counted both, a run with a single weak-tier\n    item would write a failing verdict whose detail names an observation the report\n    shows as handled.'
    hard, weak = [], []
    for r in gaps:
        ref = note_points_at_a_mechanism(r)
        (weak if ref else hard).append((r, ref))
    return hard, weak


def uncovered(rows, text, include_all=False):
    out = []
    for r in rows:
        oid = str(r.get("id"))
        if oid in EXEMPT:
            continue
        recurred = (r.get("recurrences") or 0) >= 1
        severe = r.get("severity") in ("blocker", "high")
        if not (recurred or (include_all and severe)):
            continue
        if cited(text, oid):
            continue
        out.append(r)
    
    rank = {"blocker": 0, "high": 1, "normal": 2, "low": 3}
    out.sort(key=lambda r: (-(r.get("recurrences") or 0),
                            rank.get(r.get("severity"), 9),
                            str(r.get("observed_date") or "")))
    return out


def run(argv):
    rows = observations()
    if not rows:
        print("observation-coverage: no observations found at %s" % OBS, file=sys.stderr)
        return 0
    text, files = corpus_text()
    include_all = "--all" in argv
    gaps = uncovered(rows, text, include_all)
    recurring = [r for r in rows if (r.get("recurrences") or 0) >= 1]

    if "--json" in argv:
        print(json.dumps({
            "observations": len(rows), "files_scanned": files,
            "recurring": len(recurring), "uncovered": len(gaps),
            "exempt": len(EXEMPT),
            "gaps": [{"id": r.get("id"), "recurrences": r.get("recurrences") or 0,
                      "severity": r.get("severity"), "status": r.get("status"),
                      "summary": (r.get("observation") or "")[:200]} for r in gaps],
        }, indent=1))
        return 0

    if "--health" in argv:
        
        
        
        
        record = os.path.join(hp.scripts_dir(), "health-record.py")
        if os.path.exists(record):
            hard, _weak = split_tiers(gaps)
            if hard:
                worst = hard[0][0]
                detail = ("%d recurring observation(s) have no mechanism citing them; "
                          "worst: #%s seen %sx — run observation-coverage.py"
                          % (len(hard), worst.get("id"), worst.get("recurrences")))
                
                argv = [sys.executable, record, "observation-coverage", "--fail", detail,
                        "--replace"]
            else:
                argv = [sys.executable, record, "observation-coverage", "--ok"]
            subprocess.run(argv, capture_output=True)
        return 0

    print("observation-coverage: %d observation(s), %d recurring, %d file(s) scanned.\n"
          % (len(rows), len(recurring), files))
    if not gaps:
        print("  every recurring observation is cited by a hook, script or test.")
        return 0

    hard, weak = split_tiers(gaps)
    if hard:
        print("  %d lesson(s) this store paid for more than once, with nothing "
              "mechanical\n  citing them. A third sighting is currently prevented by "
              "memory alone.\n" % len(hard))
        for r, _ in hard:
            print("  #%-4s x%-2s %-8s %-9s %s"
                  % (r.get("id"), r.get("recurrences") or 0, r.get("severity"),
                     r.get("status"), (r.get("observation") or "")[:84].replace("\n", " ")))
    else:
        print("  every recurring observation has a mechanism citing it.\n")
    if weak:
        print("\n  Weaker tier — the resolution note names an existing mechanism file, "
              "but nothing\n  in that file cites the observation, so a later reader "
              "cannot tell what it guards:")
        for r, ref in weak:
            print("    #%-4s x%-2s -> %s" % (r.get("id"), r.get("recurrences") or 0, ref))
        print("    Close these by naming the observation number in that file.")
    if not hard and not weak:
        return 0
    print("\n  Close one by adding a check to invariant-check.py, a hook with a test, or\n"
          "  a server test -- and name the observation number in it, which is how this\n"
          "  report sees it. If it genuinely cannot be mechanized, add it to EXEMPT with\n"
          "  the reason, so the claim is reviewable instead of silent.")
    return 0


def main():
    try:
        return run(sys.argv[1:])
    except Exception as exc:                       
        print("observation-coverage: failed: %r" % (exc,), file=sys.stderr)
        return 0


if __name__ == "__main__":
    store_task.main_or_forward("observation-coverage", main)
