#!/usr/bin/env python3
'eval-case-candidates — the behaviors the fleet actually gets wrong, ranked, and\nwhich of them no eval case measures.\n\nWhy this shape. Audit observations are a poor source of eval cases: most recurring\nones describe daemons, MCP bridges, materialization races and hook parsers, and an\neval case can only measure what an agent does in a session. Mining them would produce\na mostly irrelevant report, and such a report is one people learn to skim.\n\nThe better signal is a hook refusal: a recorded instance of the agent trying to do\nsomething the rules forbid, with the correct outcome already known. Refusals are the\nbehavioral failure record, ranked by how often each failure happens, which is the\norder an eval suite should be built in.\n\nCoverage is declared, never inferred. A case names the guard it exercises in a `covers`\nlist. Matching case prose against hook names was considered and rejected: it produces\nconfident false coverage, and a suite that believes it measures something it does not is\nworse than one with a visible hole. Same reasoning as the invariant allowlists -- an\nexemption must be written down to be reviewable.\n\nRead a big number both ways. A guard firing hundreds of times is a candidate for a case,\nand it is also evidence the rule may be unlearnable as written. A case makes that\nvisible as a floor that never rises however the instructions are reworded, which is the\nsignal to change the rule rather than repeat it.\n\nObservations guarded: #471, #473, #495.'
import json
import os
import re
import subprocess
import sys

STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")
SCRIPTS = os.path.join(STORE, "global", "scripts")



MIN_FIRINGS = 20





EXEMPT = {
    "require-structured-questions":
        "fires when options are put in prose instead of AskUserQuestion, and "
        "AskUserQuestion does not exist in `claude -p`. The agent has no legal "
        "alternative in a single-shot run, so a case would measure the harness's own "
        "limitation and fail forever. Its hook test covers the refusal path.",
    "block-secret-read":
        "reachable only by authoring a fixture that tempts an agent into dumping "
        "credential-shaped content, which is a bad thing to have sitting in a case file "
        "and to re-run on every baseline. The guard has a deny/allow hook test; what an "
        "eval would add is not worth writing that fixture.",
    "subagent-return-gate":
        "fires on SubagentStop, and eval cases run one agent with no delegation. There "
        "is nothing in a fixture that could reach it.",
    "block-worktree-move-mid-flight":
        "fires on EnterWorktree or ExitWorktree only while a subagent launched by the "
        "same session is still running, and eval cases run one agent with no "
        "delegation, so no fixture can put a subagent in flight. Its "
        "hook test covers the refusal path.",
    "audit-observation-guard":
        "fires only on this store's own add_/update_/resolve_audit_observation MCP "
        "calls, meaningful only in a session working against this store. eval-run's "
        "fixtures are throwaway temp repos with no store identity to file an "
        "observation about, so no coding task here ever calls that tool at all, not "
        "even down the well-behaved path.",
    "block-consent-self-grant":
        "guards the git-write, write-outside-home and test-unlock consent scripts and "
        "grant files, which exist only on a machine running this store's own hook "
        "fleet, keyed to this store's state dir. A generic eval fixture has no consent "
        "flow to grant into, so nothing in a coding task ever touches this path.",
    "approval-question":
        "fires only on an AskUserQuestion call in the exact approval shape used to let "
        "user grant a token by picking Approve, a live exchange between the agent and "
        "user. A case's scripted judge cannot play user's side of that dialog without "
        "the case measuring its own canned answer instead of the agent.",
    "block-instruction-budget-overrun":
        "fires only on an always-loaded upsert_instruction to this store that goes over "
        "the byte budget. eval-run's fixtures are throwaway repos with no store "
        "instructions, so no coding task there writes one. Its hook test covers the "
        "refusal path.",
    "require-investigation-before-edit":
        "denies the first edit to each file by design, and its approved criteria say "
        "so. A refusal is the rule working, not an agent mistake, so a case would only "
        "measure that the gate exists.",
    "memory-husk-guard":
        "fires only on memory and store-entity writes through this store's MCP tools, "
        "which a throwaway-repo fixture never makes.",
    "require-worktree-add-location":
        "every refusal on record came from fixtures testing the hook itself; no agent "
        "put a worktree in the wrong place. Its "
        "hook test covers the refusal path.",
    "memory-capture-on-correction":
        "needs user correcting the agent mid-session, which a single `claude -p` run "
        "cannot contain, and eval-run stands it down in every agent run because a case "
        "prompt's \"never\" is not a correction. Its hook test and "
        "hook-test-cases cover the turn logic.",
}


def firings(days=None):
    'Refusal counts per hook, from hook-firing-report — one source, never a second scan.\n\n    `--days` is passed through because the full-history scan is ~45s over 4 GB, and\n    running it unwindowed inside verify-quality-system pushed that whole suite past two\n    minutes for a number the windowed scan already had.'
    argv = [sys.executable, os.path.join(SCRIPTS, "hook-firing-report.py")]
    if days:
        argv += ["--days", str(days)]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip()[:200] or "hook-firing-report failed")
    out = proc.stdout or ""
    counts = {}
    for line in out.splitlines():
        m = re.match(r"\s+([a-z][a-z0-9-]+)\s+(\d+)\s+\w", line)
        if m:
            counts[m.group(1)] = int(m.group(2))
    return counts


def _load_cases():
    'The case list, executed rather than imported — the filename carries a dash.'
    path = os.path.join(SCRIPTS, "eval-cases.py")
    ns = {}
    exec(compile(open(path, encoding="utf-8").read(), path, "exec"), ns)
    return ns.get("CASES") or []


def main():
    days = None
    min_firings = MIN_FIRINGS
    as_json = "--json" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--days" and i + 1 < len(sys.argv):
            days = int(sys.argv[i + 1])
        
        
        
        
        if a == "--min" and i + 1 < len(sys.argv):
            min_firings = int(sys.argv[i + 1])
    counts = firings(days)
    if as_json:
        cs = _load_cases() if counts else []
        covered = {g for c in cs for g in (c.get("covers") or [])}
        print(json.dumps({
            "days": days, "min": min_firings,
            "gaps": {n: c for n, c in sorted(counts.items(), key=lambda kv: -kv[1])
                     if c >= min_firings and n not in covered and n not in EXEMPT},
            "covered": {n: counts.get(n, 0) for n in sorted(covered)},
            "exempt": sorted(n for n in EXEMPT if n in counts),
            "no_data": not counts,
        }))
        return 0
    if not counts:
        
        
        
        
        print("eval-case-candidates: no refusal data on this machine — no session "
              "transcripts here, so there is nothing to rank. Not a failure.")
        return 0
    cs = _load_cases()
    covered = set()
    for c in cs:
        for g in c.get("covers") or []:
            covered.add(g)

    print("eval-case-candidates: %d case(s), %d guard(s) with refusals on record.\n"
          % (len(cs), len(counts)))

    gaps = sorted([(n, c) for n, c in counts.items()
                   if c >= min_firings and n not in covered and n not in EXEMPT],
                  key=lambda kv: -kv[1])
    if gaps:
        print("  MOST-VIOLATED RULES WITH NO EVAL CASE — ranked by real frequency:")
        for name, n in gaps:
            print("    %-32s %5d refusal(s)" % (name, n))
        print()
        print("    Each is a behavior the fleet gets wrong often, with a known-correct")
        print("    outcome, and nothing measuring whether a scaffold change improves it.")
        print("    Read a big number both ways: the rule may be unlearnable as written,")
        print("    which a case would show as a floor that never rises.")

    have = [(n, counts.get(n, 0)) for n in sorted(covered)]
    if have:
        print("\n  Already measured: %s"
              % ", ".join("%s (%d)" % (n, c) for n, c in have))

    ex = [(n, counts.get(n, 0)) for n in sorted(EXEMPT) if n in counts]
    if ex:
        print("\n  Exempt — cannot be measured by this harness, reason on file:")
        for n, c in ex:
            print("    %-32s %5d   %s" % (n, c, EXEMPT[n].split(",")[0][:44]))

    thin = [n for n, c in counts.items()
            if c < min_firings and n not in covered and n not in EXEMPT]
    if thin:
        print("\n  Below the %d-refusal floor, not worth a case yet (%d): %s"
              % (min_firings, len(thin), ", ".join(sorted(thin))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
