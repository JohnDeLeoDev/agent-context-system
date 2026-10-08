#!/usr/bin/env python3
"eval-selftest — can each eval case actually fail?\n\nTHE FAILURE THIS CATCHES. A `file_contains` whose pattern is ALREADY in the fixture\npasses before the agent does anything. So does a `command_succeeds` that the untouched\nfixture already satisfies. The case then scores green forever, contributes a confident\n100% to the baseline, and measures nothing — the permanently-green check this store\nalready has a rule about, wearing the costume of a passing test.\n\nIt is not hypothetical. In one evening this suite produced: a `file_lacks` that failed a\ncorrectly-fixed file because a guard above a line leaves the line present; a scorer that\ncounted `__pycache__` from a test run the prompt ASKED for as unrequested edits; and a\n`file_contains` on a path the agent had correctly written inside a worktree. Every one\nwas found by paying for a run and reading the output afterward. All of them were\ndecidable for free, beforehand, from the case definition and the fixture.\n\nWHAT IT ASSERTS\n\n  * the check type is one the runner implements -- a typo makes an unknown type, and\n    the runner returns False for unknown types, so the case fails for a reason that has\n    nothing to do with the agent\n  * every `path` names a file the fixture creates, or is plainly a file the case expects\n    the agent to create\n  * `file_contains` does NOT already match the untouched fixture (a case that cannot fail)\n  * `command_succeeds` does NOT already succeed against the untouched fixture, and its\n    command is at least syntactically valid shell\n  * `tool_used`/`tool_not_used` name a plausible tool\n  * every case has a `why`, an `id` that is unique, and either checks or a judge\n\nIt runs each case's fixture into a real temp dir and evaluates the checks against it, so\nthis is the SAME code path the paid run uses -- not a re-implementation that could drift.\n\n  eval-selftest.py           report\n  eval-selftest.py --json\nExit 1 if any case has a problem; 0 when the suite can genuinely fail."
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
KNOWN_TOOLS = {"Bash", "Read", "Edit", "Write", "Grep", "Glob", "Task", "Agent",
               "NotebookEdit", "WebFetch", "WebSearch", "TodoWrite", "MultiEdit"}


def load(name):
    path = os.path.join(HERE, name)
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load spec for %s from %s" % (name, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build(case, root):
    for rel, content in (case.get("fixture") or {}).items():
        dest = os.path.join(root, rel)
        os.makedirs(os.path.dirname(dest) or root, exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(content)


def audit_case(case, runner):
    'Problems with this case, as strings. Empty list means it can genuinely fail.'
    problems = []
    fixture = case.get("fixture") or {}
    if not case.get("why"):
        problems.append("no `why` — an ungrounded case measures an imagined agent")
    
    
    
    
    
    
    if case.get("judge_led") and not str(case.get("judge_led")).strip():
        problems.append("judge_led is set but says nothing — give the reason")
    if not (case.get("checks") or case.get("judge")):
        problems.append("nothing scores it")
    if case.get("prompt") and case.get("turns"):
        problems.append("has both `prompt` and `turns`; `turns` wins, so one is dead")
    if not case.get("prompt") and not case.get("turns"):
        problems.append("nothing drives the agent")

    root = tempfile.mkdtemp(prefix="evalselftest-")
    try:
        build(case, root)
        for spec in (case.get("checks") or []):
            kind = spec.get("type")
            if kind in ("file_contains", "file_lacks"):
                if "path" not in spec or "pattern" not in spec:
                    problems.append("%s missing path/pattern" % kind)
                    continue
                if spec["path"] not in fixture:
                    problems.append("%s names %s, which the fixture does not create"
                                    % (kind, spec["path"]))
            elif kind in ("tool_used", "tool_not_used"):
                if spec.get("name") not in KNOWN_TOOLS:
                    problems.append("%s names an unknown tool %r"
                                    % (kind, spec.get("name")))
            elif kind == "command_succeeds":
                cmd = spec.get("cmd")
                if not cmd:
                    problems.append("command_succeeds with no cmd")
                    continue
                syn = subprocess.run(["bash", "-n", "-c", cmd],
                                     capture_output=True, text=True)
                if syn.returncode != 0:
                    problems.append("command_succeeds is not valid shell: %s"
                                    % (syn.stderr or "").strip()[:80])
            elif kind in runner.CHECK_TYPES:
                pass                    
            else:
                problems.append("unknown check type %r — the runner will fail it for "
                                "reasons unrelated to the agent" % kind)

        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        deterministic = [s for s in (case.get("checks") or [])
                         if s.get("type") in runner.DETERMINISTIC]
        if deterministic and not problems:
            verdicts = []
            for spec in deterministic:
                try:
                    ok, _detail = runner.check(spec, root, "")
                except Exception as exc:                     
                    problems.append("%s raised on the bare fixture: %r"
                                    % (spec.get("type"), exc))
                    ok = False
                verdicts.append(bool(ok))
            if verdicts and all(verdicts) and not case.get("judge_led"):
                problems.append(
                    "doing NOTHING satisfies every deterministic check (%s) — only the "
                    "judge can fail this case, so its mechanical layer measures nothing"
                    % ", ".join(sorted({s["type"] for s in deterministic})))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return problems


def main(argv):
    cases = load("eval-cases.py").CASES
    runner = load("eval-run.py")
    seen, report = set(), []
    for case in cases:
        cid = case.get("id") or "<unnamed>"
        problems = audit_case(case, runner)
        if cid in seen:
            problems.append("duplicate id")
        seen.add(cid)
        if problems:
            report.append((cid, problems))

    if "--json" in argv:
        print(json.dumps({"cases": len(cases), "with_problems": len(report),
                          "problems": {c: p for c, p in report}}, indent=1))
        return 1 if report else 0

    print("eval-selftest: %d case(s) checked.\n" % len(cases))
    if not report:
        print("  every case is well-formed, and every deterministic check FAILS against "
              "its own\n  untouched fixture — which is what makes a pass mean something.")
        return 0
    for cid, problems in report:
        print("  %s" % cid)
        for p in problems:
            print("      - %s" % p)
    print("\n  A case that cannot fail is worse than no case: it contributes a confident "
          "100%\n  to the baseline while measuring nothing.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:                                  
        print("eval-selftest: failed: %r" % (exc,), file=sys.stderr)
        sys.exit(2)
