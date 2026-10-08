#!/usr/bin/env python3
"deps-check: report which declared dependencies this machine lacks.\n\nReads global/deps/manifest.toml, picks this machine's roles by its chezmoi machine id, runs each\ntool's `check` argv and reports one line per problem: `<tool> missing on <machine> (role <r>):\n<fix>`. Statuses: ok, missing (not on PATH, or its `expect` text is absent), broken (on PATH but\nthe check exits nonzero or times out), below_floor, unknown_version (output has no version; never\na failure). A launcher (`shim`) that is broken on a machine that does not carry the tool is listed\nas `unexpected`, informational only.\n\nIt is read-only and local: no network, no install, no shell. The only commands it runs are the\nmanifest's `check` argvs, each capped by a timeout, and health-record.py. The `fix` text is never\nexecuted. Output redacts home directories, so a report that travels to fleet health carries no\nusername.\n\nIt writes ~/.local/state/agent-context/deps.json (the report; the fleet row and the session-start\nbanner read it) and records the outcome through health-record.py (component `deps`), which\npreflight-core-health.py already prints at session start. home-materialize.py starts it detached\nwhen deps.json is more than 12 hours old.\n\nUsage:\n  deps-check.py [--json] [--machine ID] [--manifest PATH] [--state-dir DIR] [--timeout SECS]\n                [--no-record]\n  deps-check.py -h | --help\n\nExit status: 0 nothing wrong; 3 a dependency is missing, broken or below its floor, the state file\nis unwritable, or this Python cannot read the manifest; 2 unreadable or invalid manifest, unknown\nmachine, bad arguments; 4 another run holds the lock.\n\nThe syntax stays valid on Python 3.8 (the Synology system Python), so an old interpreter reaches\nthe message about tomllib and not a traceback."
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

try:
    import tomllib
except ImportError:  
    tomllib = None

try:
    import fcntl
except ImportError:  
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness_paths as hp  
import store_mcp  

SCHEMA = 1
DEFAULT_MANIFEST = os.path.normpath(os.path.join(HERE, "..", "deps", "manifest.toml"))
CHANNELS = ("manual", "brew", "apt", "opkg", "winget", "dotnet", "uv-python", "uv-tool",
            "node-tools-sync", "relay-update")
OSES = ("mac", "ubuntu", "debian", "synology", "windows")
TOOL_KEYS = frozenset({"check", "check_any", "timeout", "floor", "exists_only", "expect",
                       "presence_only", "shim", "verify_only", "channel", "fix", "os",
                       "package", "node_package"})
MAX_TOOL_TIMEOUT = 60
MAX_ALTERNATIVES = 4
NOT_ON_PATH = "not on PATH"
RELAY_ROLES = ("relay", "relay-client")
CHECK_TIMEOUT = 10.0
NAME_RE = re.compile(r"^[A-Za-z0-9._@+-]+$")
VERSION_RE = re.compile(r"\d+(?:\.\d+)+")
HOME_RE = re.compile(r"(?:/Users|/home|/var/services/homes)/[^/\s:'\"]+")
WIN_HOME_RE = re.compile(r"[A-Za-z]:[\\/]Users[\\/][^\\/\s:'\"]+")
EXTRA_PATH = ("~/.local/bin", "/opt/homebrew/bin", "~/.dotnet/tools", "/usr/local/bin",
              "/snap/bin")
DOTNET_ENV = {"DOTNET_NOLOGO": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
              "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1"}
STATE_FILE = "deps.json"
LOG_FILE = "deps-check.log"
LOCK_FILE = "deps-check.lock"




UPLOAD_PLACEHOLDER_UUID = "00000000-0000-4000-8000-000000000000"
HEALTH_RECORD = os.path.join(HERE, "health-record.py")
HEALTH_COMPONENT = "deps"
DETAIL_LIMIT = 100


class ManifestError(Exception):
    'The manifest cannot be used; `args[0]` is a list of one-line problems.'


class MachineError(Exception):
    "This machine's id is unknown or has no manifest entry."


def redact(text, home):
    '`text` with the home directory and any /Users, /home or Synology home path shortened to ~.'
    if home and home != "/":
        text = re.sub(re.escape(home) + r"(?![\w.-])", "~", text)
    return HOME_RE.sub("~", WIN_HOME_RE.sub("~", text))


def parse_version(text):
    'The first dotted version in `text` as a tuple of ints, or None.'
    match = VERSION_RE.search(text or "")
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


def load_manifest(path):
    if tomllib is None:
        raise ManifestError(["this interpreter (%d.%d) cannot read the manifest (tomllib needs 3.11 "
                             "or newer); run: uv python install 3.14 --default" % sys.version_info[:2]])
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except OSError as exc:
        raise ManifestError(["%s: cannot read (%s)" % (path, exc.strerror or exc)])
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(["%s: not valid TOML (%s)" % (path, exc)])


def _argv_errors(name, label, argv):
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(a, str) and a and not any(c in a for c in "\0\n\r") for a in argv)):
        return ["tool %s: %s must be a non-empty list of strings without line breaks" % (name, label)]
    if not (NAME_RE.fullmatch(argv[0]) or argv[0].startswith(("/", "~/"))):
        return ["tool %s: %s[0] %r is not a plain command name or path" % (name, label, argv[0])]
    return []


def alternatives(tool):
    'The commands that can prove the tool works, in order: `check`, or each `check_any` entry.'
    return [tool["check"]] if "check" in tool else list(tool["check_any"])


def _fix_ok(fix):
    if isinstance(fix, str):
        return bool(fix.strip())
    return isinstance(fix, dict) and isinstance(fix.get("default"), str) and bool(fix["default"].strip())


def validate(manifest):
    'Every problem in the manifest, one line each; [] when it is usable. A structure this\n    function does not anticipate is reported as a problem, never as an exception.'
    try:
        return _validate(manifest)
    except (TypeError, KeyError, AttributeError, ValueError) as exc:
        return ["manifest structure is invalid (%s: %s)" % (type(exc).__name__, exc)]


def _validate(manifest):
    errors = []
    if (not isinstance(manifest, dict) or type(manifest.get("schema")) is not int
            or manifest.get("schema") != SCHEMA):
        return ["schema must be %d" % SCHEMA]
    tools = manifest.get("tool") if isinstance(manifest.get("tool"), dict) else {}
    roles = manifest.get("role") if isinstance(manifest.get("role"), dict) else {}
    machines = manifest.get("machine") if isinstance(manifest.get("machine"), dict) else {}
    for label, table in (("tool", tools), ("role", roles), ("machine", machines)):
        if not table:
            errors.append("no [%s.*] tables" % label)
    for name, tool in sorted(tools.items()):
        if not isinstance(tool, dict):
            errors.append("tool %s: not a table" % name)
            continue
        for key in sorted(set(tool) - TOOL_KEYS):
            errors.append("tool %s: unknown key %s" % (name, key))
        if ("check" in tool) == ("check_any" in tool):
            errors.append("tool %s: give exactly one of check and check_any" % name)
        elif "check" in tool:
            errors.extend(_argv_errors(name, "check", tool["check"]))
        else:
            alts = tool["check_any"]
            if not isinstance(alts, list) or not alts:
                errors.append("tool %s: check_any must be a non-empty list of commands" % name)
            elif len(alts) > MAX_ALTERNATIVES:
                errors.append("tool %s: check_any holds at most %d commands" % (name, MAX_ALTERNATIVES))
            else:
                for i, argv in enumerate(alts):
                    errors.extend(_argv_errors(name, "check_any[%d]" % i, argv))
        limit = tool.get("timeout")
        if limit is not None and not (isinstance(limit, (int, float)) and not isinstance(limit, bool)
                                      and 0 < limit <= MAX_TOOL_TIMEOUT):
            errors.append("tool %s: timeout must be a number of seconds, above 0 and at most %d"
                          % (name, MAX_TOOL_TIMEOUT))
        if tool.get("channel") not in CHANNELS:
            errors.append("tool %s: channel %r is not one of %s" % (name, tool.get("channel"),
                                                                   ", ".join(CHANNELS)))
        if not _fix_ok(tool.get("fix")):
            errors.append("tool %s: fix is missing" % name)
        floor = tool.get("floor")
        if floor is not None and not (isinstance(floor, str) and parse_version(floor)):
            errors.append("tool %s: floor %r is not a dotted version" % (name, floor))
        for key in ("exists_only", "presence_only", "shim", "verify_only"):
            if key in tool and not isinstance(tool[key], bool):
                errors.append("tool %s: %s must be true or false" % (name, key))
        tool_os = tool.get("os")
        if tool_os is not None and not (isinstance(tool_os, list)
                                        and all(o in OSES for o in tool_os)):
            errors.append("tool %s: os must be a list drawn from %s" % (name, ", ".join(OSES)))
        if "expect" in tool and not isinstance(tool["expect"], str):
            errors.append("tool %s: expect must be a string" % name)
        for key in ("package", "node_package"):
            if key in tool and not (isinstance(tool[key], str) and tool[key].strip()):
                errors.append("tool %s: %s must be a non-empty string" % (name, key))
        if tool.get("channel") == "node-tools-sync" and "node_package" not in tool:
            errors.append("tool %s: a node-tools-sync tool needs node_package" % name)
        if tool.get("channel") == "relay-update" and tool.get("verify_only") is not True:
            errors.append("tool %s: a relay-update tool must be verify_only" % name)
    for role, body in sorted(roles.items()):
        listed = body.get("tools") if isinstance(body, dict) else None
        if not isinstance(listed, list):
            errors.append("role %s: tools must be a list" % role)
            continue
        for name in listed:
            if name not in tools:
                errors.append("role %s: tool %s is not defined" % (role, name))
            elif role in RELAY_ROLES and isinstance(tools[name], dict) \
                    and tools[name].get("verify_only") is not True:
                errors.append("role %s: tool %s must be verify_only" % (role, name))
    for mid, body in sorted(machines.items()):
        if not isinstance(body, dict) or body.get("os") not in OSES:
            errors.append("machine %s: os must be one of %s" % (mid, ", ".join(OSES)))
            continue
        listed_roles = body.get("roles")
        if not isinstance(listed_roles, list) or not listed_roles:
            errors.append("machine %s: roles must be a non-empty list" % mid)
            continue
        for role in listed_roles:
            if role not in roles:
                errors.append("machine %s: role %s is not defined" % (mid, role))
                continue
            for name in roles[role].get("tools", []) if isinstance(roles[role], dict) else []:
                spec = tools.get(name)
                applies = spec.get("os") if isinstance(spec, dict) else None
                if applies is not None and body["os"] not in applies:
                    errors.append("machine %s: role %s lists %s, which does not apply to os %s"
                                  % (mid, role, name, body["os"]))
    unmanaged = manifest.get("unmanaged", {})
    if not isinstance(unmanaged, dict):
        errors.append("unmanaged must be a table of name = reason")
    else:
        for uname, reason in sorted(unmanaged.items()):
            if not (isinstance(reason, str) and reason.strip()):
                errors.append("unmanaged %s: give a reason" % uname)
            if uname in tools:
                errors.append("unmanaged %s is also declared as a tool" % uname)
    return errors


def machine_id_from_chezmoi(home):
    'The machine_id chezmoi persisted, or None.'
    cfg = os.path.join(home, ".config", "chezmoi", "chezmoi.toml")
    try:
        with open(cfg, encoding="utf-8") as fh:
            for line in fh:
                match = re.match(r'\s*machine_id\s*=\s*"([^"]+)"', line)
                if match:
                    return match.group(1)
    except OSError:
        pass
    return None


def fix_for(tool, os_id):
    fix = tool.get("fix")
    if isinstance(fix, dict):
        return fix.get(os_id) or fix["default"]
    return fix


def check_env(env, home):
    out = dict(env)
    extra = [p.replace("~", home, 1) if p.startswith("~") else p for p in EXTRA_PATH]
    have = [p for p in out.get("PATH", "").split(os.pathsep) if p]
    out["PATH"] = os.pathsep.join(have + [p for p in extra if p not in have])
    out.update(DOTNET_ENV)
    return out


def resolve(name, env, home):
    if name.startswith("~/"):
        name = os.path.join(home, name[2:])
    if os.path.isabs(name):
        
        
        suffixes = ("", ".cmd", ".exe", ".bat") if os.name == "nt" else ("",)
        for suffix in suffixes:
            path = name + suffix
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return None
    return shutil.which(name, path=env.get("PATH", ""))


def run_check(argv, exe, env, timeout, home):
    '(returncode, output) or (None, reason) when the command could not finish.'
    try:
        done = subprocess.run([exe] + list(argv[1:]), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                              timeout=timeout, shell=False,
                              cwd=home if home and os.path.isdir(home) else None)
    except subprocess.TimeoutExpired:
        return None, "timed out after %gs" % timeout
    except OSError as exc:
        return None, redact(exc.strerror or "cannot run", home)
    return done.returncode, done.stdout.decode("utf-8", "replace")


def _first_line(text, home):
    for line in text.splitlines():
        if line.strip():
            return redact(line.strip(), home)[:DETAIL_LIMIT]
    return ""


def check_tool(tool, env, timeout, home):
    "{status, found, detail} for one tool. With `check_any` the first alternative that is on\n    PATH decides, ok or not, the way a caller that takes the first command it finds would; only a\n    command that is not on PATH moves on to the next alternative. The tool's `timeout`, when set,\n    replaces the run's `--timeout` for each alternative."
    if "timeout" in tool:
        timeout = tool["timeout"]
    result = None
    for argv in alternatives(tool):
        result = _check_one(dict(tool, check=argv), env, timeout, home)
        if not (result["status"] == "missing" and result["detail"] == NOT_ON_PATH):
            break
    return result


def _check_one(tool, env, timeout, home):
    argv = tool["check"]
    exe = resolve(argv[0], env, home)
    if exe is None:
        return {"status": "missing", "found": None, "detail": NOT_ON_PATH}
    if tool.get("exists_only"):
        return {"status": "ok", "found": None, "detail": ""}
    code, output = run_check(argv, exe, env, timeout, home)
    if code is None:
        return {"status": "broken", "found": None, "detail": output}
    if code != 0:
        detail = "exit %d" % code
        line = _first_line(output, home)
        return {"status": "broken", "found": None, "detail": detail + (": " + line if line else "")}
    expect = tool.get("expect")
    if expect is not None and expect not in output:
        return {"status": "missing", "found": None, "detail": "%s is not listed" % expect}
    if tool.get("presence_only"):
        return {"status": "ok", "found": None, "detail": ""}
    found = parse_version(output)
    floor = tool.get("floor")
    if found is None:
        return {"status": "unknown_version" if floor else "ok", "found": None, "detail": ""}
    text = ".".join(str(part) for part in found)
    if floor and found < parse_version(floor):
        return {"status": "below_floor", "found": text, "detail": ""}
    return {"status": "ok", "found": text, "detail": ""}


def evaluate(manifest, machine, env, timeout=CHECK_TIMEOUT, home=None, now=None):
    'The full report for `machine`. Raises MachineError for an id the manifest lacks.'
    home = home or hp.home()
    entry = manifest["machine"].get(machine)
    if entry is None:
        raise MachineError("machine id %r has no entry in the manifest (known: %s)"
                           % (machine, ", ".join(sorted(manifest["machine"]))))
    tools = manifest["tool"]
    
    
    os_id = "windows" if os.name == "nt" else entry["os"]
    needed = {}
    for role in entry["roles"]:
        for name in manifest["role"][role]["tools"]:
            applies = tools[name].get("os")
            if name not in needed and (applies is None or os_id in applies):
                needed[name] = role
    env = check_env(env, home)
    results, problems = {}, []
    for name in sorted(needed):
        tool, role = tools[name], needed[name]
        res = check_tool(tool, env, timeout, home)
        res["role"] = role
        res["floor"] = tool.get("floor")
        results[name] = res
        status, fix = res["status"], fix_for(tool, os_id)
        if status == "missing":
            problems.append("%s missing on %s (role %s): %s" % (name, machine, role, fix))
        elif status == "broken":
            problems.append("%s broken on %s (role %s), %s: %s"
                            % (name, machine, role, res["detail"], fix))
        elif status == "below_floor":
            problems.append("%s %s is below %s on %s (role %s): %s"
                            % (name, res["found"], res["floor"], machine, role, fix))
    unexpected = []
    for name in sorted(tools):
        tool = tools[name]
        if name in needed or not tool.get("shim"):
            continue
        argv = alternatives(tool)[0]
        exe = resolve(argv[0], env, home)
        if exe is None:
            continue
        code, _ = run_check(argv, exe, env, timeout, home)
        if code != 0:
            unexpected.append(name)
    bucket = {"missing": [], "broken": [], "below_floor": []}
    for name, res in results.items():
        if res["status"] in bucket:
            bucket[res["status"]].append(name)
    fleet = {"ok": not problems, "missing": bucket["missing"], "broken": bucket["broken"],
             "below_floor": bucket["below_floor"]}
    return {"schema": SCHEMA, "machine": machine, "os": os_id,
            "at": int(time.time() if now is None else now), "ok": not problems,
            "tools": results, "problems": problems, "unexpected": unexpected, "fleet": fleet}


def write_state(state_dir, report):
    'Write deps.json atomically. False when the directory is not writable.'
    path = os.path.join(state_dir, STATE_FILE)
    tmp = path + ".tmp"
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def write_log(state_dir, text):
    "Replace deps-check.log with this run's report, so the log always matches deps.json.\n\n    Every run writes the log. If only home-materialize's detached spawn wrote it (as the\n    child's stdout), a run started any other way would refresh deps.json and leave an\n    older failure in the log. The replace is atomic; a spawn's stdout keeps writing to the old, now unlinked file."
    path = os.path.join(state_dir, LOG_FILE)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def record_health(report, env):
    'Hand the outcome to health-record.py so the next session start prints it.'
    if report["problems"]:
        text = "%d dependency problem(s) on %s:\n%s" % (
            len(report["problems"]), report["machine"], "\n".join(report["problems"]))
        args = ["--fail", text, "--replace"]
    else:
        args = ["--ok"]
    try:
        subprocess.run([sys.executable, HEALTH_RECORD, HEALTH_COMPONENT] + args,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, env=env, timeout=CHECK_TIMEOUT, shell=False)
    except (OSError, subprocess.SubprocessError):
        pass


def human(report, wrote):
    tools = report["tools"]
    if report["ok"]:
        lines = ["deps-check: %s: all %d tools ok" % (report["machine"], len(tools))]
    else:
        lines = ["deps-check: %s: %d problem(s)" % (report["machine"], len(report["problems"]))]
        lines += report["problems"]
    for name, res in sorted(tools.items()):
        if res["status"] == "unknown_version":
            lines.append("note: %s version could not be read" % name)
    for name in report["unexpected"]:
        lines.append("note: %s is on PATH but broken, and this machine does not carry it" % name)
    if not wrote:
        lines.append("state unwritable: could not write %s" % STATE_FILE)
    return "\n".join(lines)


def relay_env(home, env=None):
    "AGENT_CONTEXT_HOST/TOKEN/PORT via store_mcp.relay_env, keyed off `home` (hp.home()), not\n    the raw environment's HOME, in case they differ (e.g. no HOME on Windows)."
    env = os.environ if env is None else env
    return store_mcp.relay_env(dict(env, HOME=home))


def upload_deps(env, report, home):
    'Upload `report` via relay_report(kind="deps"). Raises on any failure; the caller decides\n    what that means for the local run (deps-check.py itself must never fail or change output over\n    an upload fault).'
    body = dict(report)
    body["hostname"] = socket.gethostname()
    body["home_dir"] = home
    store_mcp.call("relay_report", {"kind": "deps", "uuid_hint": UPLOAD_PLACEHOLDER_UUID,
                                    "body": json.dumps(body)}, env=env)


def store_is_checkout(root):
    "Is `root` a real store checkout (has its own server/ package or a .git), rather than a\n    relay-only host's materialized bundle? Same test as token-usage-collect.py's own check:\n    `.exists()`, not a directory check, because a git WORKTREE's `.git` is a file, not a dir."
    return os.path.exists(os.path.join(root, ".git")) or os.path.isdir(os.path.join(root, "server"))


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="deps-check.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--machine", help="machine id (default: chezmoi's machine_id)")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--state-dir", help="where deps.json and the lock go")
    parser.add_argument("--timeout", type=float, default=CHECK_TIMEOUT,
                        help="seconds allowed per check command")
    parser.add_argument("--no-record", action="store_true",
                        help="print only: write no deps.json and record no health verdict")
    return parser.parse_args(argv)


def main(argv=None, env=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ if env is None else env)
    home = hp.home(env.get("HOME"))
    try:
        manifest = load_manifest(args.manifest)
        errors = validate(manifest)
        if errors:
            raise ManifestError(errors)
    except ManifestError as exc:
        for line in exc.args[0]:
            print("deps-check: manifest: %s" % redact(line, home), file=sys.stderr)
        return 3 if tomllib is None else 2
    own_id = machine_id_from_chezmoi(home)
    machine = args.machine or own_id
    if args.machine and own_id and args.machine != own_id and not args.no_record:
        
        print("deps-check: note: --machine %s is not this machine (%s); nothing is recorded"
              % (args.machine, own_id), file=sys.stderr)
        args.no_record = True
    if not machine:
        print("deps-check: machine id unknown: chezmoi has no machine_id; pass --machine",
              file=sys.stderr)
        return 2
    state_dir = args.state_dir or hp.state_dir(home)
    lock = None
    if not args.no_record and fcntl is not None:
        try:
            os.makedirs(state_dir, exist_ok=True)
            lock = open(os.path.join(state_dir, LOCK_FILE), "w")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("deps-check: busy: another run holds the lock", file=sys.stderr)
            return 4
        except OSError:
            lock = None  
    try:
        try:
            report = evaluate(manifest, machine, env, timeout=args.timeout, home=home)
        except MachineError as exc:
            print("deps-check: %s" % exc, file=sys.stderr)
            return 2
        wrote = True
        if not args.no_record:
            wrote = write_state(state_dir, report)
            if wrote:
                write_log(state_dir, human(report, wrote))
                record_health(report, env)
                root = os.path.normpath(os.path.join(HERE, "..", ".."))
                if not store_is_checkout(root):
                    renv = relay_env(home, env)
                    if renv["AGENT_CONTEXT_HOST"]:
                        try:
                            upload_deps(renv, report, home)
                        except Exception as exc:
                            print("deps-check: upload to the daemon failed: %s"
                                 % redact(str(exc), home), file=sys.stderr)
        print(json.dumps(report, indent=2, sort_keys=True) if args.json else human(report, wrote))
        return 0 if report["ok"] and wrote else 3
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
