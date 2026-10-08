#!/usr/bin/env python3
"deps-sync: apply a declared fix for a fleet dependency, in user space only.\n\nReads global/deps/manifest.toml through deps-check.py (imported by path) and, for a tool this\nmachine's roles need that deps-check reports missing or below_floor, runs the manifest's `fix`\ncommand IF AND ONLY IF: the tool is not verify_only, carries no `package` key (chezmoi's path),\nits channel is one that installs into the user's own tool cache (uv-tool, node-tools-sync), and\nthe parsed fix command's first word is one of a small allowed list (uv, python3.14, python3,\ndotnet: a whitelist, not a blacklist of installers, so a shell wrapper or a full path cannot\nsmuggle a privileged command through). A python fix must run a named .py script, never `-c`.\nEvery other problem is reported, never touched: chezmoi, brew, apt and the relay's own tools\nstay owned by their existing provisioning.\n\nDry-run by default. --apply requires --all or one or more tool names. A tool whose process\nlooks live (a `pgrep -f` match; best effort, never a hard guarantee) is skipped, not replaced.\nCoordinates with the relay updater's own lock file (~/.cache/agent-context/relay-update.lock,\nthe same exclusive-create scheme agent_context.relay_update uses) so a fix never runs while\nthat updater is mid-install on the same host. Refuses --apply outright on a host whose home\nfilesystem is read-only (rp during its SD card fault, audit #390); a dry run still works there.\n\nUsage:\n  deps-sync.py [--machine ID] [--manifest PATH] [--state-dir DIR] [--timeout SECS]\n               [--apply (--all | TOOL [TOOL ...])]\n  deps-sync.py -h | --help\n\nExit status: 0 nothing to apply, or (with --apply) every selected fix verified ok; 2 manifest or\nmachine error, or a bad argument; 3 a problem is unapplied (dry run), or a fix failed, was\nskipped, or did not verify; 4 busy (this script's own lock, or the relay updater's, is held)."
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness_paths as hp  

_spec = importlib.util.spec_from_file_location("deps_check", os.path.join(HERE, "deps-check.py"))
deps_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deps_check)

try:
    import fcntl
except ImportError:  
    fcntl = None

ALLOWED_CHANNELS = frozenset({"uv-tool", "node-tools-sync"})



ALLOWED_COMMANDS = frozenset({"uv", "python3.14", "python3", "dotnet"})
STATE_FILE = "deps-sync.json"
LOCK_FILE = "deps-sync.lock"
RELAY_LOCK_STALE_SECONDS = 600
APPLY_TIMEOUT = 300.0
_TRAILING_PAREN = re.compile(r"^(.*\S)\s+\([^()]*\)\s*$")


def strip_comment(text):
    'The fix text with a trailing parenthetical aside removed ("uv tool install ruff (needs\n    uv)"), but only when the parenthetical is the last thing on the line: a paren that has real\n    content after it (part of a quoted argument, say) is left untouched.'
    match = _TRAILING_PAREN.match(text)
    return match.group(1) if match else text.strip()


def parse_fix(text):
    'The fix text as an argv, or None when it does not parse as a plain command line.'
    try:
        argv = shlex.split(strip_comment(text))
    except ValueError:
        return None
    return argv or None


def eligible(tool, os_id):
    '(True, "") when deps-sync may apply this tool\'s fix on this OS; else (False, reason).'
    if tool.get("verify_only"):
        return False, "verify_only: owned by the relay updater"
    if "package" in tool:
        return False, "carries a package key: owned by chezmoi"
    channel = tool.get("channel")
    if channel not in ALLOWED_CHANNELS:
        return False, "channel %r is not user-space installable" % channel
    tool_os = tool.get("os")
    if tool_os is not None and os_id not in tool_os:
        return False, "does not apply to os %s" % os_id
    return True, ""


def fix_argv(tool, os_id, home):
    '(argv, "") ready to run, or (None, reason).'
    ok, why = eligible(tool, os_id)
    if not ok:
        return None, why
    fix = deps_check.fix_for(tool, os_id)
    argv = parse_fix(fix)
    if not argv:
        return None, "fix text %r does not parse as a command" % fix
    argv = [os.path.join(home, a[2:]) if a.startswith("~/") else a for a in argv]
    head = os.path.basename(argv[0])
    if head not in ALLOWED_COMMANDS:
        return None, "fix command %r is not on the allowed list" % argv[0]
    if head in ("python3.14", "python3"):
        arg1 = argv[1] if len(argv) > 1 else ""
        if arg1.startswith("-") or not arg1.endswith(".py"):
            return None, "a python fix command must run a .py script, not %r" % arg1
    return argv, ""


def process_running(name):
    'Best effort: True only when `pgrep -f` can be run and finds a match. A miss here is not\n    proof the tool is idle; a hit skips the fix rather than risk replacing a live binary.'
    pgrep = deps_check.shutil.which("pgrep")
    if not pgrep:
        return False
    try:
        done = subprocess.run([pgrep, "-f", name], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=5, shell=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def relay_lock_path(home):
    return os.path.join(home, ".cache", "agent-context", "relay-update.lock")


def take_relay_lock(home):
    'Create the shared lock exclusively, the way agent_context.relay_update does, so a fix\n    never runs while that updater is mid-install. A lock older than RELAY_LOCK_STALE_SECONDS\n    belongs to a run that never cleaned up, and is reclaimed.'
    lock = relay_lock_path(home)
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    for _ in range(2):
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return lock
        except FileExistsError:
            try:
                if time.time() - os.stat(lock).st_mtime < RELAY_LOCK_STALE_SECONDS:
                    return None
                os.unlink(lock)
            except OSError:
                return None
    return None


def release_relay_lock(lock):
    try:
        os.unlink(lock)
    except OSError:
        pass


def refresh_relay_lock(lock):
    "Move the lock's mtime back to now, so a run applying several fixes back to back never\n    lets the relay updater's own stale-lock reclaim (same threshold, separate code) treat this\n    still-live lock as abandoned and start a relay update mid-fix."
    try:
        os.utime(lock, None)
    except OSError:
        pass


def home_is_read_only(home):
    try:
        st = os.statvfs(home)
    except (OSError, AttributeError):
        return False
    return bool(st.f_flag & os.ST_RDONLY)


def plan(manifest, machine, env, home, timeout=None):
    'The deps-check report for `machine`, plus one plan row per problem tool: {tool, status,\n    action ("apply"/"skip"), reason, argv}. Never runs a fix.'
    report = deps_check.evaluate(manifest, machine, env,
                                 timeout=timeout or deps_check.CHECK_TIMEOUT, home=home)
    os_id = report["os"]
    tools = manifest["tool"]
    rows = []
    for name, res in sorted(report["tools"].items()):
        if res["status"] not in ("missing", "below_floor"):
            continue
        argv, reason = fix_argv(tools[name], os_id, home)
        if argv is None:
            rows.append({"tool": name, "status": res["status"], "action": "skip", "reason": reason,
                         "argv": None})
        elif process_running(name):
            rows.append({"tool": name, "status": res["status"], "action": "skip",
                         "reason": "a %s process is running" % name, "argv": argv})
        else:
            rows.append({"tool": name, "status": res["status"], "action": "apply", "reason": "",
                         "argv": argv})
    return report, rows


def apply_one(row, env, home, timeout):
    '(ok, detail) for one plan row already marked "apply".'
    argv = row["argv"]
    exe = deps_check.resolve(argv[0], env, home)
    if exe is None:
        return False, "%s: not on PATH" % argv[0]
    code, output = deps_check.run_check(argv, exe, env, timeout, home)
    if code is None:
        return False, output
    if code != 0:
        return False, "exit %d: %s" % (code, deps_check._first_line(output, home))
    return True, ""


def verify_one(manifest, machine, env, home, name):
    "The tool's status after a fix, re-checked the same way deps-check would."
    tool = manifest["tool"][name]
    env2 = deps_check.check_env(env, home)
    res = deps_check.check_tool(tool, env2, deps_check.CHECK_TIMEOUT, home)
    return res["status"]


def write_log(state_dir, entries):
    path = os.path.join(state_dir, STATE_FILE)
    tmp = path + ".tmp"
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"at": int(time.time()), "runs": entries}, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="deps-sync.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--machine", help="machine id (default: chezmoi's machine_id)")
    parser.add_argument("--manifest", default=deps_check.DEFAULT_MANIFEST)
    parser.add_argument("--state-dir", help="where the run log and the lock go")
    parser.add_argument("--timeout", type=float, default=APPLY_TIMEOUT,
                        help="seconds allowed per applied fix command")
    parser.add_argument("--apply", action="store_true", help="run the planned fixes (default: report only)")
    parser.add_argument("--all", action="store_true", help="with --apply, apply every planned fix")
    parser.add_argument("tools", nargs="*", help="with --apply, the tool names to fix")
    return parser.parse_args(argv)


def main(argv=None, env=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ if env is None else env)
    home = hp.home(env.get("HOME"))
    if args.apply and not args.all and not args.tools:
        print("deps-sync: --apply needs --all or one or more tool names", file=sys.stderr)
        return 2
    try:
        manifest = deps_check.load_manifest(args.manifest)
        errors = deps_check.validate(manifest)
        if errors:
            raise deps_check.ManifestError(errors)
    except deps_check.ManifestError as exc:
        for line in exc.args[0]:
            print("deps-sync: manifest: %s" % deps_check.redact(line, home), file=sys.stderr)
        return 2
    own_id = deps_check.machine_id_from_chezmoi(home)
    machine = args.machine or own_id
    if not machine:
        print("deps-sync: machine id unknown: chezmoi has no machine_id; pass --machine",
              file=sys.stderr)
        return 2
    state_dir = args.state_dir or hp.state_dir(home)
    lock = None
    if fcntl is not None:
        try:
            os.makedirs(state_dir, exist_ok=True)
            lock = open(os.path.join(state_dir, LOCK_FILE), "w")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("deps-sync: busy: another run holds the lock", file=sys.stderr)
            return 4
        except OSError:
            lock = None
    relay_lock = None
    try:
        try:
            report, rows = plan(manifest, machine, env, home)
        except deps_check.MachineError as exc:
            print("deps-sync: %s" % exc, file=sys.stderr)
            return 2
        if not args.apply:
            print(json.dumps({"machine": machine, "plan": rows}, indent=2, sort_keys=True))
            return 0 if not rows else 3
        if home_is_read_only(home):
            print("deps-sync: %s: home filesystem is read-only; refusing --apply" % machine,
                  file=sys.stderr)
            return 3
        selected = {r["tool"] for r in rows} if args.all else set(args.tools)
        unmatched = sorted(selected - {r["tool"] for r in rows}) if not args.all else []
        if unmatched:
            print("deps-sync: %s: named tool(s) not in the current plan (no problem, or not in "
                  "the manifest for this machine): %s" % (machine, ", ".join(unmatched)),
                  file=sys.stderr)
        relay_lock = take_relay_lock(home)
        if relay_lock is None:
            print("deps-sync: busy: the relay updater's lock is held", file=sys.stderr)
            return 4
        entries, ok_all = [], not unmatched
        for row in rows:
            if row["tool"] not in selected:
                continue
            entry = {"tool": row["tool"], "before": row["status"]}
            if row["action"] != "apply":
                entry.update(argv=row["argv"], result="skipped", reason=row["reason"], after=row["status"])
                entries.append(entry)
                ok_all = False
                refresh_relay_lock(relay_lock)
                continue
            applied_ok, detail = apply_one(row, env, home, args.timeout)
            after = verify_one(manifest, machine, env, home, row["tool"])
            entry.update(argv=row["argv"], applied_ok=applied_ok, detail=detail, after=after)
            entry["result"] = "applied" if applied_ok and after == "ok" else "failed"
            ok_all = ok_all and entry["result"] == "applied"
            entries.append(entry)
            refresh_relay_lock(relay_lock)
        write_log(state_dir, entries)
        print(json.dumps({"machine": machine, "applied": entries}, indent=2, sort_keys=True))
        return 0 if ok_all else 3
    finally:
        if lock is not None:
            lock.close()
        if relay_lock is not None:
            release_relay_lock(relay_lock)


if __name__ == "__main__":
    sys.exit(main())
