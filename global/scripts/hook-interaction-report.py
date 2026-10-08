#!/usr/bin/env python3
'hook-interaction-report — which guards are MASKED by another that refuses first.\n\nWHY. hook-test-run exercises one hook against one payload, in isolation. Production runs\nthe whole chain for that event, so a refusal from an earlier hook can mean a later one\nnever speaks -- and its message is the part carrying the actionable remedy. Every hook\ncan be individually green while, in practice, nobody ever reads what one of them says.\n\nWHAT PROMPTED IT, stated accurately because the first draft of this comment got it\nwrong. guard-git-write\'s push-to-deploy gate really was invisible on the deny/allow axis\n-- a push from a main checkout is refused anyway, so the gate could have been dead for\nweeks with every test still passing. But that was ordering WITHIN one hook, not one hook\nmasking another, and checking it here is what showed the difference. The inter-hook\nversion is the same failure one level up, and nothing was looking for it.\n\nMASKING IS NOT AUTOMATICALLY A BUG. Two guards refusing one thing is defense in depth\nand often deliberate. What matters is that the message which actually REACHES the agent\nis the one with the useful remedy. So this reports relationships and does not fail on\nthem.\n\nA CLEAN RUN HERE IS ONLY WORTH SOMETHING BECAUSE OF --selftest. No payload in the\ncurrent case set is refused by two hooks at all, so there is no live case to control\nagainst, and "no guard is masked" would otherwise be indistinguishable from a report\nthat cannot detect masking. The selftest builds a synthetic chain with a stub that\nalways denies and asserts all three directions.'
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")
SCRIPTS = os.path.join(STORE, "global", "scripts")
HOOKS = hp.hooks_dir()


def registered():
    'event -> [(hook filename, matcher)], in the order settings.json lists them.\n\n    THE MATCHER IS PART OF THE CHAIN. The first version ignored it and ran every hook\n    registered for the event, so a `Read` payload was fed to require-worktree-edit\n    (matcher `Write|Edit|MultiEdit|NotebookEdit`) -- a hook the harness would never\n    invoke for that tool. It reported two guards as masked by a refusal that cannot\n    happen, which is the false positive this whole store treats as worse than no report.'
    out = {}
    
    
    
    path = hp.settings_file()
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return out
    
    
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "hook_registry", os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook-registry.py"))
    if spec is None or spec.loader is None:
        raise ImportError("cannot load hook-registry.py")
    hook_registry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook_registry)
    for event, groups in hook_registry.effective_hooks(path, cfg).items():
        for g in groups:
            for h in g.get("hooks") or []:
                m = re.search(r'hooks/([A-Za-z0-9._-]+\.(?:sh|py))', h.get("command") or "")
                if m:
                    out.setdefault(event, []).append((m.group(1), g.get("matcher") or ""))
    return out


def applies(matcher, payload):
    'Would the harness actually invoke a hook with this matcher for this payload?'
    if not matcher:
        return True                      
    tool = (payload or {}).get("tool_name") or ""
    try:
        return bool(re.fullmatch(matcher, tool)) or bool(re.search(matcher, tool))
    except re.error:
        return True                      


def load_cases():
    path = os.path.join(SCRIPTS, "hook-test-cases.py")
    ns = {}
    exec(compile(open(path, encoding="utf-8").read(), path, "exec"), ns)
    for v in ns.values():
        if isinstance(v, dict) and any(k.endswith((".sh", ".py")) for k in v):
            return v
    return {}


def denies(hook_file, payload):
    p = os.path.join(HOOKS, hook_file)
    if not os.path.exists(p):
        return False
    runner = [sys.executable, p] if hook_file.endswith(".py") else ["bash", p]
    try:
        proc = subprocess.run(runner, input=json.dumps(payload), capture_output=True,
                              text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode == 2:
        return True
    try:
        doc = json.loads(proc.stdout or "{}", strict=False)
    except ValueError:
        return False
    out = doc.get("hookSpecificOutput") or {}
    return (str(out.get("permissionDecision") or "").lower() == "deny"
            or str(doc.get("decision") or "").lower() == "block")


def masked(chain, payload, hook_file):
    "The hooks refusing this payload, in chain order, when the hook under test is NOT\n    first. Split out so the report's ability to FIRE can be proven -- see selftest.\n\n    `chain` is [(name, matcher)] or [name]; the second form is what the selftest passes."
    pairs = [(h, "") if isinstance(h, str) else h for h in chain]
    refusing = [h for h, mt in pairs if applies(mt, payload) and denies(h, payload)]
    others = [h for h in refusing if h != hook_file]
    return refusing if (others and refusing and refusing[0] != hook_file) else None


def selftest():
    import shutil
    import tempfile
    global HOOKS
    tmp = tempfile.mkdtemp(prefix="hir-selftest-")
    real = HOOKS
    try:
        HOOKS = tmp
        for name, body in (("always-deny.sh", "echo no >&2\nexit 2\n"),
                           ("never-deny.sh", "exit 0\n"),
                           ("under-test.sh", "echo no >&2\nexit 2\n")):
            with open(os.path.join(tmp, name), "w") as fh:
                fh.write("#!/usr/bin/env bash\n" + body)
        payload = {"tool_name": "Bash", "tool_input": {"command": "x"}}
        bad = 0
        for label, chain, want in (
                ("masked by an earlier hook -> REPORTED",
                 ["always-deny.sh", "under-test.sh"], ["always-deny.sh", "under-test.sh"]),
                ("under test refuses FIRST -> silent",
                 ["under-test.sh", "always-deny.sh"], None),
                ("only the hook under test refuses -> silent",
                 ["never-deny.sh", "under-test.sh"], None)):
            got = masked(chain, payload, "under-test.sh")
            ok = got == want
            bad += not ok
            print("  %-46s %s" % (label, "ok" if ok else "WRONG: %r" % (got,)))
        print("\nselftest: %d wrong" % bad)
        return 1 if bad else 0
    finally:
        HOOKS = real
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    if "--selftest" in sys.argv:
        return selftest()
    reg = registered()
    cases = load_cases()
    event_of = {}
    for ev, pairs in reg.items():
        for n, _mt in pairs:
            event_of.setdefault(n, ev)

    findings = []
    n_checked = 0
    for hook_file, specs in sorted(cases.items()):
        ev = event_of.get(hook_file)
        if not ev:
            continue
        chain = reg.get(ev) or []
        for spec in specs:
            if spec.get("expect") != "deny":
                continue
            n_checked += 1
            refusing = masked(chain, spec.get("payload") or {}, hook_file)
            if refusing:
                findings.append((hook_file, spec.get("name", "?"), refusing))

    print("hook-interaction-report: %d deny case(s) replayed against their full chain.\n"
          % n_checked)
    if not findings:
        print("  No guard is masked: in every case the hook under test is the FIRST to")
        print("  refuse, so its message is the one the agent actually reads.")
        print("  (Run --selftest to confirm this report can detect masking at all.)")
        return 0

    print("  MASKED — another guard refuses first, so this hook's message is not what")
    print("  the agent sees:")
    for hook_file, name, refusing in findings:
        print("    %s" % hook_file)
        print("      case  : %s" % name[:70])
        print("      chain : %s" % " -> ".join(refusing))
    print()
    print("  Not automatically a bug: two guards refusing one thing is defense in depth.")
    print("  What matters is that the message reaching the agent carries the remedy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
