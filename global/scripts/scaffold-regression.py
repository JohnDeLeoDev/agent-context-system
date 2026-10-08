#!/usr/bin/env python3
"scaffold-regression — did that change to the harness make things better or worse?\n\nWhy this exists. Scaffolding complexity tends to grow while quality stays flat, and\ncontext management, the layer this store edits most, is the one most prone to\nregression. A stuck agent burns its budget in unproductive edit-test loops, so token\nweight is a leading indicator of quality as well as a cost line.\n\nThis file implements automated regression testing on non-functional qualities (token\nconsumption, tool overhead): a CI suite catches functional breakage and is blind to\nbehavioral regression. Without it, an instruction, hook or skill edit ships on judgment\nalone.\n\nWhat it measures, and why these five:\n\n  tokens/request     total context weight moved per API call. Rises when instructions,\n                     tool rosters or transcripts grow. The headline number.\n  output/request     how much the model writes per call. Rises with over-verification\n                     and with narration; a known Opus 5 failure mode.\n  tool calls/request tool overhead. Rises when the agent thrashes in an edit-test\n                     loop.\n  tool error rate    the most direct failure proxy available without a trace, and the\n                     one that moves first when a guard starts misfiring. Hook refusals\n                     are excluded (see refusal rate): guard tightening alone would\n                     otherwise read as a regression with no tool failing more.\n  refusal rate       how often a hook denied or rewrote a call, tracked on its own line\n                     so enforcement stays visible without reading as a failure. A rise\n                     here is still worth a look: it is either guards getting stricter\n                     (fine) or an agent repeatedly reaching for a path that keeps getting\n                     refused (a real behavior problem, not the guard's).\n\nHow to read it. This is observational, not an experiment: workload varies day to day, so\na single window comparison is evidence, not proof. Treat a >15% move as worth\ninvestigating and a >30% move as needing an explanation before the change stays. The\nhonest use is paired with a known change date via --at.\n\nObservations guarded: #371."
import datetime as dt
import glob
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp
import store_task  

STORE = os.environ.get("AGENT_CONTEXT_STORE", os.path.expanduser("~/.agent-context"))
ROLLUPS = os.path.join(STORE, "machines", "*", "token-usage", "*.json")
HEALTH_RECORD = os.path.join(hp.scripts_dir(), "health-record.py")

WARN = 15.0      
ALERT = 30.0     


def _rows():
    "(usage rows, tool rows) merged across every machine's rollups."
    usage, tools = [], []
    for path in sorted(glob.glob(ROLLUPS)):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        for key, sink in (("usage", usage), ("tools", tools)):
            block = doc.get(key) or {}
            cols = block.get("columns") or []
            for row in block.get("rows") or []:
                if len(row) == len(cols):
                    sink.append(dict(zip(cols, row)))
    return usage, tools


def _window(rows, lo, hi):
    return [r for r in rows if lo <= str(r.get("day", "")) < hi]


def _metrics(usage, tools):
    'The five numbers, or None where the window has no evidence.'
    req = sum(r.get("requests", 0) or 0 for r in usage)
    if not req:
        return None
    total = sum((r.get("input", 0) or 0) + (r.get("cache_w5", 0) or 0)
                + (r.get("cache_w1h", 0) or 0) + (r.get("cache_read", 0) or 0)
                + (r.get("output", 0) or 0) for r in usage)
    out = sum(r.get("output", 0) or 0 for r in usage)
    calls = sum(r.get("calls", 0) or 0 for r in tools)
    errs = sum(r.get("errors", 0) or 0 for r in tools)
    
    
    
    
    refs = sum(r.get("refusals", 0) or 0 for r in tools)
    return {
        "requests": req,
        "tokens_per_request": total / req,
        "output_per_request": out / req,
        "tool_calls_per_request": calls / req,
        "tool_error_rate": ((errs - refs) / calls) if calls else 0.0,
        "refusal_rate": (refs / calls) if calls else 0.0,
    }





LABELS = [
    ("tokens_per_request", "tokens / request", "{:,.0f}"),
    ("output_per_request", "output tokens / request", "{:,.0f}"),
    ("tool_calls_per_request", "tool calls / request", "{:.2f}"),
    ("tool_error_rate", "tool error rate", "{:.3%}"),
    ("refusal_rate", "refusal rate", "{:.3%}"),
]


def run(argv):
    args = argv[1:]

    def opt(name, default=None):
        return args[args.index(name) + 1] if name in args else default

    window = int(opt("--window", "7"))
    pivot = opt("--at")
    as_json = "--json" in args
    do_health = "--health" in args

    end = (dt.date.fromisoformat(pivot) if pivot else dt.date.today())
    
    
    b_lo = (end - dt.timedelta(days=window)).isoformat()
    b_hi = end.isoformat()
    a_lo = b_hi
    a_hi = (end + dt.timedelta(days=window)).isoformat()

    usage, tools = _rows()
    before = _metrics(_window(usage, b_lo, b_hi), _window(tools, b_lo, b_hi))
    after = _metrics(_window(usage, a_lo, a_hi), _window(tools, a_lo, a_hi))

    if not before or not after:
        msg = ("scaffold-regression: not enough evidence (%s..%s: %s, %s..%s: %s)"
               % (b_lo, b_hi, "data" if before else "none",
                  a_lo, a_hi, "data" if after else "none"))
        print(json.dumps({"error": msg}) if as_json else msg)
        return 0

    deltas, worst = [], 0.0
    for key, label, fmt in LABELS:
        b, a = before[key], after[key]
        pct = ((a - b) / b * 100.0) if b else 0.0
        deltas.append({"metric": key, "label": label, "before": b, "after": a,
                       "pct": pct, "fmt": fmt})
        worst = max(worst, pct)

    if as_json:
        print(json.dumps({"before_window": [b_lo, b_hi], "after_window": [a_lo, a_hi],
                          "before_requests": before["requests"],
                          "after_requests": after["requests"],
                          "deltas": deltas, "worst_pct": worst}, indent=1))
    else:
        print("scaffold-regression: %s..%s (%d req) -> %s..%s (%d req)"
              % (b_lo, b_hi, before["requests"], a_lo, a_hi, after["requests"]))
        print("  (all metrics are lower-is-better; + is a regression)")
        for d in deltas:
            flag = "  ALERT" if d["pct"] >= ALERT else ("  warn" if d["pct"] >= WARN else "")
            print("  %-26s %14s -> %14s  %+7.1f%%%s"
                  % (d["label"], d["fmt"].format(d["before"]),
                     d["fmt"].format(d["after"]), d["pct"], flag))
        if worst < WARN:
            print("  no regression beyond %.0f%%." % WARN)

    if do_health and os.path.exists(HEALTH_RECORD):
        if worst >= ALERT:
            bad = "; ".join("%s %+.0f%%" % (d["label"], d["pct"])
                            for d in deltas if d["pct"] >= WARN)
            subprocess.run(
                [sys.executable, HEALTH_RECORD, "scaffold-regression", "--fail",
                 "harness efficiency regressed after %s: %s. "
                 "Run: scaffold-regression.py" % (b_hi, bad)],
                check=False)
        else:
            subprocess.run([sys.executable, HEALTH_RECORD, "scaffold-regression", "--ok"],
                           check=False)
    return 0


def main():
    try:
        return run(sys.argv)
    except Exception as exc:                  
        print("scaffold-regression: failed: %r" % (exc,), file=sys.stderr)
        return 0


if __name__ == "__main__":
    store_task.main_or_forward("scaffold-regression", main)
