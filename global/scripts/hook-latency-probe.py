#!/usr/bin/env python3
'hook-latency-probe: what the PreToolUse hook chain costs per tool call.\n\nOnly PreToolUse is run. PostToolUse and Stop hooks commit, push and write session\nclaims, so running them on a sample payload would change the store.\n\nLeaving no trace. Hooks keep per-session state: block-redundant-read writes a ledger\ndirectory named by session id, and records a repeat read in the shared\nread-telemetry.jsonl. With one session id shared by every run, later runs would\nmeasure the repeat-read refusal and leave ledger state behind. So every run gets its\nown id, and no read is a repeat; afterwards every state path named by one of those ids is\nremoved, and any file under the state roots that still mentions one is named on stderr\nwith exit 3. Shared files are never rewritten, because a live session may be appending.\n\nA hook that exits 2 on a sample is a guard refusing it, which is normal and still timed.\nA hook that runs past the timeout is killed, reported as "timeout", and the run goes on.\n\nUsage:\n  hook-latency-probe.py [--runs N] [--json] [--settings PATH]\n  hook-latency-probe.py -h | --help\n\nExit: 0 measured and clean; 1 bad settings; 2 bad flag; 3 measured but state left behind.'

import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

STORE = os.environ.get("AGENT_CONTEXT_STORE", os.path.expanduser("~/.agent-context"))
DEFAULT_SETTINGS = hp.settings_file()
STATE_ROOTS = (
    hp.state_dir(),
    os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
                 "agent-context"),
)
TIMEOUT_SECS = 10
SESSION_PREFIX = "hooklatency-%d-" % os.getpid()
USAGE = "usage: hook-latency-probe.py [--runs N] [--json] [--settings PATH]"

SAMPLES = {
    "Bash": {"command": "git -C %s status --short" % STORE, "description": "status"},
    "Edit": {"file_path": os.path.join(STORE, ".agents", "tmp", "hook-latency-probe.txt"),
             "old_string": "a", "new_string": "b"},
    "Read": {"file_path": os.path.join(STORE, "README.md")},
}


def parse_args(argv):
    opts = {"runs": 3, "json": False, "settings": DEFAULT_SETTINGS}
    args = list(argv)
    while args:
        a = args.pop(0)
        if a in ("-h", "--help"):
            print((__doc__ or "").strip())
            sys.exit(0)
        elif a == "--json":
            opts["json"] = True
        elif a in ("--runs", "--settings"):
            if not args:
                sys.exit("hook-latency-probe: %s needs a value\n%s" % (a, USAGE))
            value = args.pop(0)
            if a == "--runs":
                if not value.isdigit() or int(value) < 1:
                    sys.exit("hook-latency-probe: --runs takes a whole number of 1 or more\n" + USAGE)
                opts["runs"] = int(value)
            else:
                opts["settings"] = os.path.expanduser(value)
        else:
            print("hook-latency-probe: unknown flag %s\n%s" % (a, USAGE), file=sys.stderr)
            sys.exit(2)
    return opts


def load_pre_tool_use(path):
    if not os.path.isfile(path):
        print("hook-latency-probe: no settings file at %s" % path, file=sys.stderr)
        sys.exit(1)
    try:
        with open(path, encoding="utf-8") as fh:
            settings = json.load(fh)
    except (OSError, ValueError) as exc:
        print("hook-latency-probe: cannot read %s: %s" % (path, exc), file=sys.stderr)
        sys.exit(1)
    return (settings.get("hooks") or {}).get("PreToolUse") or []


def matches(matcher, tool):
    if matcher in (None, "", "*"):
        return True
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return matcher == tool


def hook_name(command):
    found = re.findall(r"([\w.-]+\.(?:sh|py))", command)
    return found[0] if found else command[:40]


class Sessions:
    'Hands out one throwaway session id per hook run.'

    def __init__(self):
        self.count = 0

    def next(self):
        self.count += 1
        return SESSION_PREFIX + str(self.count)


def time_hook(argv, payload, env):
    'One run: (milliseconds, status).'
    start = time.perf_counter()
    try:
        proc = subprocess.run(argv, input=payload, capture_output=True, text=True,
                              env=env, cwd=STORE, timeout=TIMEOUT_SECS)
        code = proc.returncode
        status = "ok" if code == 0 else "exit " + str(code)
    except subprocess.TimeoutExpired:
        status = "timeout"
    return (time.perf_counter() - start) * 1000, status


def probe(groups, runs, sessions):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=STORE)
    report = {}
    for tool, tool_input in SAMPLES.items():
        rows = []
        for group in groups:
            if not matches(group.get("matcher"), tool):
                continue
            for hook in group.get("hooks") or []:
                command = hook.get("command")
                if not command:
                    continue
                name = hook_name(command)
                print("[%s] %s" % (tool, name), file=sys.stderr, flush=True)
                times, statuses = [], []
                for _ in range(runs):
                    payload = json.dumps({"session_id": sessions.next(), "transcript_path": "",
                                          "cwd": STORE, "hook_event_name": "PreToolUse",
                                          "tool_name": tool, "tool_input": tool_input})
                    ms, status = time_hook(["/bin/sh", "-c", command], payload, env)
                    times.append(ms)
                    statuses.append(status)
                worst = next((s for s in statuses if s == "timeout"),
                             next((s for s in statuses if s != "ok"), "ok"))
                rows.append({"name": name, "median_ms": round(statistics.median(times), 1),
                             "status": worst})
        rows.sort(key=lambda r: r["median_ms"], reverse=True)
        report[tool] = {"processes": len(rows),
                        "total_ms": round(sum(r["median_ms"] for r in rows), 1),
                        "hooks": rows}
    return report


def clean_up(started):
    "Remove state paths named by this run's ids; return files that still mention one."
    for root in STATE_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
            for d in list(dirnames):
                if SESSION_PREFIX in d:
                    shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)
                    dirnames.remove(d)
            for f in filenames:
                if SESSION_PREFIX in f:
                    try:
                        os.remove(os.path.join(dirpath, f))
                    except OSError:
                        pass
    left = []
    for root in STATE_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for f in filenames:
                path = os.path.join(dirpath, f)
                try:
                    if os.path.getmtime(path) < started:
                        continue
                    with open(path, encoding="utf-8", errors="ignore") as fh:
                        if SESSION_PREFIX in fh.read():
                            left.append(path)
                except OSError:
                    continue
    return left


def main(argv):
    opts = parse_args(argv[1:])
    groups = load_pre_tool_use(opts["settings"])
    started = time.time()
    report = probe(groups, opts["runs"], Sessions())
    left = clean_up(started)
    if opts["json"]:
        print(json.dumps({"event": "PreToolUse", "runs": opts["runs"],
                          "settings": opts["settings"], "tools": report,
                          "state_left_behind": left}, indent=1))
    else:
        print("hook-latency-probe: PreToolUse, median of %d run(s) per hook, %s"
              % (opts["runs"], opts["settings"]))
        for tool, data in report.items():
            print("\n%-5s %2d hook process(es), chain %.0f ms"
                  % (tool, data["processes"], data["total_ms"]))
            for row in data["hooks"]:
                print("  %7.1f ms  %-8s %s" % (row["median_ms"], row["status"], row["name"]))
    if left:
        print("hook-latency-probe: state left behind, still naming %s*:\n  %s"
              % (SESSION_PREFIX, "\n  ".join(left)), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
