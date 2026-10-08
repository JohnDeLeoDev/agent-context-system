#!/usr/bin/env python3
'hook-firing-report — which guards are load-bearing, which have never once fired.\n\nWhy. A guard that never fires is either perfect prevention or dead code. Without a\nfiring record nothing can tell the two apart, so every hook is carried, maintained and\npaid for on faith.\n\nHow, without touching a hook. A refusal is written into the session transcript\nwith the hook\'s own path in it ("PreToolUse:Bash hook error: [/…/hooks/<name>.sh]: …"),\nso the whole history is already instrumented. The scan reads raw bytes and does not\nparse JSON, which keeps it fast over gigabytes of transcripts. Nothing to install, nothing\nto keep in sync, and it reaches sessions that ran months ago.\n\nTwo signals, because one is not enough. The path only\nappears when the harness frames a shell or edit refusal; a hook refusing an MCP tool\ncall emits "BLOCKED by <name>" with no path. Scanning paths alone lists such a\nhook as never fired, and a wrong "never fired" list is worse than no list, because it\ninvites retiring a live guard. Counts are the max of the two signals, never the sum: a shell\nrefusal usually carries both, so adding them doubles it, and each alone undercounts --\nso the larger is an honest floor.\n\nWhat it cannot see, stated because a zero is easy to over-read: only refusals leave this\ntrace. An advisory hook injecting context, and a SessionStart hook doing silent work,\nnever appear however well they work. So hooks are split by whether they have a deny path\nat all, and only the ones that can refuse and never have are worth a second look -- and\neven then the report says what it cannot settle: their test is what proves the path\nstill works.\n\nObservations guarded: #306.'
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")
HOOKS = os.path.join(STORE, "global", "hooks")
PROJECTS = hp.projects_dir()





PAT = r'hooks/[A-Za-z0-9._-]+\.(sh|py)\\?\]:'








NAMEPAT = (r'(\.(sh|py)\\?\] |\\?\]: |"content":"|Error: )(BLOCKED|Blocked) by [`]?[a-z][a-z0-9-]+'
           r'|context: REWROTE by [`]?[a-z][a-z0-9-]+')






GUARDPAT = r'(hook-dispatch\.py [A-Za-z]+\\?\]: |\\n\\n)\[[a-z][a-z0-9-]+\.(sh|py)\] '



DENIES = re.compile(r'permissionDecision"?\s*:\s*"?deny|"deny"|exit\s+2\b|'
                    r'decision"?\s*:\s*"?block', re.I)


def can_deny(name):
    for ext in (".sh", ".py"):
        p = os.path.join(HOOKS, name + ext)
        if os.path.exists(p):
            try:
                return bool(DENIES.search(open(p, encoding="utf-8", errors="replace").read()))
            except OSError:
                return False
    return None            


def registered():
    'Every hook the harness actually runs, by bare name -> set of events.'
    import json
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
                m = re.search(r'hooks/([A-Za-z0-9._-]+)\.(?:sh|py)', h.get("command") or "")
                if m:
                    out.setdefault(m.group(1), set()).add(event)
    return out


def scan(days=None):
    if not os.path.isdir(PROJECTS):
        return {}, 0
    cmd = ["grep", "-roh", "--include=*.jsonl", "-E", PAT, PROJECTS]
    if days:
        
        
        cutoff = time.time() - days * 86400
        files = []
        for root, _dirs, names in os.walk(PROJECTS):
            for n in names:
                if not n.endswith(".jsonl"):
                    continue
                p = os.path.join(root, n)
                try:
                    if os.path.getmtime(p) >= cutoff:
                        files.append(p)
                except OSError:
                    pass
        if not files:
            return {}, 0
        cmd = ["grep", "-oh", "-E", PAT] + files

    def run(pattern):
        'Same grep invocation, one pattern swapped in.'
        argv = [pattern if x is PAT else x for x in cmd]
        out = subprocess.run(argv, capture_output=True, text=True).stdout or ""
        c = {}
        for line in out.splitlines():
            guard = re.search(r'\[([a-z][a-z0-9-]+)\.(?:sh|py)\] $', line)
            if pattern is GUARDPAT and guard:
                n = guard.group(1)
            else:
                n = re.sub(r'^hooks/|\.(sh|py)\\?\]:$', "", line)
                n = re.sub(r'^.*?(BLOCKED|Blocked|REWROTE) by [`]?', "", n)
            c[n] = c.get(n, 0) + 1
        return c

    by_path, by_name, by_guard = run(PAT), run(NAMEPAT), run(GUARDPAT)
    counts = {k: max(by_path.get(k, 0), by_name.get(k, 0), by_guard.get(k, 0))
              for k in set(by_path) | set(by_name) | set(by_guard)}
    n_files = len([1 for _r, _d, ns in os.walk(PROJECTS) for n in ns if n.endswith(".jsonl")])
    return counts, n_files


def main():
    days = None
    for i, a in enumerate(sys.argv):
        if a == "--days" and i + 1 < len(sys.argv):
            days = int(sys.argv[i + 1])
    counts, n_files = scan(days)
    reg = registered()
    
    
    
    
    counts = {k: v for k, v in counts.items() if k in reg or can_deny(k) is not None}
    window = "last %d day(s)" % days if days else "all history"

    print("hook-firing-report: %d refusal(s), floor, across %d transcript(s), %s\n"
          % (sum(counts.values()), n_files, window))

    if counts:
        print("  FIRING — these guards are load-bearing:")
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            where = ",".join(sorted(reg.get(name, {"unregistered"})))
            print("    %-32s %5d   %s" % (name, n, where))

    silent = [n for n in sorted(reg) if n not in counts and can_deny(n) is True]
    if silent:
        print("\n  NEVER REFUSED, though they can — verify each is still reachable:")
        for n in silent:
            print("    %-32s        %s" % (n, ",".join(sorted(reg[n]))))
        print("    A guard that never fires is either perfect prevention or broken, and")
        print("    this report cannot tell those apart. Its test can: the deny case in")
        print("    hook-test-cases.py is what proves the refusal path still works.")

    quiet = [n for n in sorted(reg) if n not in counts and can_deny(n) is not True]
    if quiet:
        print("\n  No deny path, so silence here means nothing (%d): %s"
              % (len(quiet), ", ".join(quiet)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
