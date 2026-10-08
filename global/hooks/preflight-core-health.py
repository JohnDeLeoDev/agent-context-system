#!/usr/bin/env python3

'preflight-core-health, SessionStart: make a degraded core system impossible to miss.\n\nThe problem this solves. Every core system here fails open, and its degraded mode\nlooks like its working mode:\n\n  * agent-context MCP down  -> a session with no instructions, no memory and no\n                               guardrails, which still answers questions normally.\n  * LSP down                -> the agent falls back to grep, which still returns hits.\n  * LSP alive but stale     -> hover returns null, which reads as "no such symbol",\n                               so the agent draws a wrong conclusion confidently.\n  * materialization partial -> "(non-fatal)" on stderr, exit 0, nobody hears.\n\nNone of those raise anything. The failure surface is a human eventually noticing.\n\nThe fix. Every probe in this system writes its verdict as a JSON file under\n~/.local/state/agent-context/health/. This hook aggregates them and, when anything is wrong,\ninjects a DEGRADED block into the session\'s own context (hookSpecificOutput\n.additionalContext), where the agent reads it; stderr alone reaches nobody.\nThe agent is required by the "Core system health" global instruction to\nreport it before doing dependent work. It also prints a systemMessage so user sees\nit in the terminal, and pushes one throttled notification per day so a machine that\nis degraded across every session for a week says so.\n\nStaleness is a finding, and so is silence. A probe file that is missing or older\nthan its max age is reported as loudly as a failure. That applies to a verdict that\nsays ok: a canary which stopped running leaves its last PASS on disk\nforever, and a check nobody runs would keep answering "healthy" for a system nobody\nhas looked at. Absence is judged against a first-seen stamp so a brand-new machine,\nwhose probes have not had a turn yet, is not accused of a fault.\n\nInput  (stdin JSON): { cwd, session_id, ... }\nOutput (stdout JSON): { hookSpecificOutput: {...additionalContext}, systemMessage }'
import json
import os
import platform
import re
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

HOME = hp.home()
STATE = os.path.join(hp.state_dir(), "health")
SCRIPTS = os.path.join(HOME, ".agent-context", "global", "scripts")
CANARY_CONFIG = os.path.join(HOME, ".agent-context", "global", "lsp-canaries.json")
PROBE = os.path.join(SCRIPTS, "core-health-probe.py")
CANARY = os.path.join(SCRIPTS, "lsp-canary.py")
HOOKPROBE = os.path.join(SCRIPTS, "hook-registration-probe.py")
CONFIGSCAN = os.path.join(SCRIPTS, "config-secret-scan.py")
HEAL = os.path.join(SCRIPTS, "self-heal.py")
NOTIFY = os.path.join(HOME, ".local", "bin", "notify")

MCP_MAX_AGE = 6 * 3600        


REPROBE_TIMEOUT = 20
MCP_STALE = 36 * 3600         
LSP_STALE = 36 * 3600         
GRACE = 24 * 3600             
NOTIFY_THROTTLE = 86400       










HEAL_TIMEOUT = 95


def read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def first_seen():
    'When this machine\'s health state was first initialized.\n\n    Absence of a verdict only means "the probe is not running" once the probe has\n    had a chance to run. Without an anchor, the two are indistinguishable, and the\n    only safe reading would be the silent one, which is the failure this file\n    exists to stop. The stamp is written on the first session and never again.'
    path = os.path.join(STATE, ".first-seen")
    rec = read_json(path)
    if rec and rec.get("ts"):
        return rec["ts"]
    now = int(time.time())
    try:
        with open(path, "w") as fh:
            json.dump({"ts": now}, fh)
    except OSError:
        pass
    return now


def past_grace(anchor):
    return time.time() - anchor > GRACE


def detach(script, *args):
    "Run a probe fully detached from the session's critical path.\n\n    Both background probes are slow: `claude mcp list` dials every\n    server, and the LSP canary waits out a cold index. Neither may\n    spend the SessionStart budget, so each writes a verdict this hook reads next\n    session. One exception: a cached MCP verdict that names a fault is re-measured\n    in the foreground before it is repeated (reprobe_now). Nothing here is\n    time-critical to the second: a core system that broke mid-session is still\n    reported an hour later."
    if not os.path.exists(script):
        return
    try:
        subprocess.Popen(
            [sys.executable, script] + list(args),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError:
        pass


def reprobe_now(cwd):
    "Re-run the MCP roster probe in the foreground, bounded by REPROBE_TIMEOUT.\n\n    A cached fault is a claim about the past, the MCP twin of bridge_serving and\n    sync_fault_is_over. The detached probe's verdict is trusted for MCP_MAX_AGE, so\n    without this a fault fixed inside that window would still be announced. Only a verdict that names a\n    fault pays for this; a clean one is still refreshed in the background.\n\n    A probe that outruns the bound is killed and handed to the background, so the\n    cached verdict is reported this session and a fresh one exists for the next."
    if not os.path.exists(PROBE):
        return
    try:
        subprocess.run([sys.executable, PROBE, cwd], timeout=REPROBE_TIMEOUT,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        detach(PROBE, cwd)
    except (OSError, subprocess.SubprocessError):
        pass


def spawn_probes(cwd):
    report = read_json(os.path.join(STATE, "mcp.json"))
    age = time.time() - report["ts"] if report and report.get("ts") else None
    
    
    
    recheck = report.get("recheck_after") if report else None
    if report and (report.get("degraded") or report.get("missing")):
        reprobe_now(cwd)
    elif age is None or age >= MCP_MAX_AGE or (recheck and time.time() >= recheck):
        detach(PROBE, cwd)

    
    
    
    
    detach(CANARY, cwd)

    
    
    detach(CONFIGSCAN)

    
    
    if os.path.exists(HOOKPROBE):
        try:
            subprocess.run([sys.executable, HOOKPROBE], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass


def mcp_findings(anchor):
    out = []
    path = os.path.join(STATE, "mcp.json")
    report = read_json(path)
    if report is None:
        
        
        
        
        if past_grace(anchor):
            out.append(("MCP roster probe", "has never produced a verdict on this "
                        "machine: it is spawned every session and never writes, "
                        "so no MCP server here is checked."))
        return out
    age = time.time() - (report.get("ts") or 0)
    if age > MCP_STALE:
        out.append(("MCP roster probe", "has not produced a verdict in %d hours: "
                    "the probe is not running." % int(age // 3600)))
        return out
    if report.get("probe_error"):
        out.append(("MCP roster probe", report["probe_error"]))
    for m in report.get("missing") or []:
        out.append(("MCP %s" % m["name"],
                    "declared in mcp-servers.json (%s scope) but absent from this "
                    "harness's roster: it never launched, so its tools do not exist "
                    "in this session." % m["scope"]))
    
    
    
    seen = time.strftime("%H:%M", time.localtime(report.get("ts") or 0))
    for d in report.get("degraded") or []:
        out.append(("MCP %s" % d["name"],
                    "`claude mcp list` reported `%s` at %s (%d min ago), so its tools "
                    "may be missing from this session." % (d["status"], seen, int(age // 60))))
    return out


WORKTREE_MARKS = hp.worktree_marks()


def main_checkout(cwd):
    'A worktree\'s main checkout, or cwd unchanged when it is not in one.\n\n    Why this exists. The two halves of the "no canary verdict" judgment must resolve\n    by one rule. `expected_canaries` matches the config by prefix, so a worktree under\n    the project root is expected to have a verdict; the verdict lookup in `lsp_findings`\n    matches on exact cwd, so a verdict written in the main checkout would not count for\n    one. Under the worktree mandate a fresh worktree is created per change and has no\n    verdict of its own, so without this it would always trip the "has never written a\n    verdict" branch, and that finding tells the agent to announce a degraded language\n    server and to distrust negative symbol answers.\n\n    Normalizing here keeps the exact match in `lsp_findings`, which must stay exact:\n    prefix matching there would report every project\'s dead server in a plain ~ session.\n    And a worktree has no language server of its own: lspd binds one daemon per main\n    checkout (memory lsp-one-daemon-per-workspace), so the main checkout is the only\n    correct answer.\n\n    String-based, with no `git rev-parse --git-common-dir`: this runs on the session\'s\n    critical path, worktrees live at `<repo>/.agents/worktrees/<name>` (legacy:\n    `<repo>/.claude/worktrees/<name>`), and a subprocess per session start is a cost\n    for an answer already in the path.'
    cwd = os.path.realpath(cwd)
    cuts = [cwd.find(mark) for mark in WORKTREE_MARKS]
    cuts = [cut for cut in cuts if cut != -1]
    return cwd[:min(cuts)] if cuts else cwd


def expected_canaries(cwd):
    '{server: symbol} that must have a fresh verdict here, per lsp-canaries.json.\n\n    The symbol matters as much as the server. A verdict only says something about the\n    question it asked, so carrying the currently-configured symbol back lets\n    lsp_findings throw out a verdict measured against a different one.'
    config = read_json(CANARY_CONFIG) or {}
    cwd = os.path.realpath(cwd)
    out = {}
    for rel, entries in (config.get("canaries") or {}).items():
        root = os.path.realpath(os.path.join(HOME, rel))
        if cwd != root and not cwd.startswith(root + os.sep):
            continue
        for e in entries or []:
            if e.get("server"):
                out[e["server"]] = e.get("symbol")
    return out


def bridge_serving(cwd, server):
    "Is a bridge for this (cwd, server) up right now with a live LSP child?\n\n    A verdict file records a past moment. The harness respawns a bridge that lost its\n    start, so a failure verdict can describe a fault that healed a minute later. A\n    banner repeating it would tell the session it has no code intelligence, and the\n    documented remedy for that banner is a restart. So a recorded failure is checked\n    against the present before it is repeated.\n\n    Asked of the daemon. Under lspd every bridge runs `--lsp python3` and the server is\n    the daemon's child, so a `ps` match on the bridge process cannot tell. The process\n    that knows is the daemon for (server, main checkout): it answers $/lspd/health, and\n    a live server has a pid and is not held down. A socket nobody answers on, or a\n    daemon holding its server down, is not serving. test-preflight-core-health.py."
    import hashlib
    import socket

    digest = hashlib.sha1(os.path.realpath(main_checkout(cwd)).encode()).hexdigest()[:16]
    path = os.path.join(hp.state_dir(HOME), "lsp", "run",
                        "%s-%s.sock" % (server, digest))
    if not os.path.exists(path):
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(path)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "$/lspd/health",
                           "params": {}}).encode()
        s.sendall(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        rfh = s.makefile("rb")
        for _ in range(8):
            length = 0
            while True:
                line = rfh.readline()
                if not line:
                    return False
                line = line.strip()
                if not line:
                    break
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            msg = json.loads(rfh.read(length) or b"null")
            if isinstance(msg, dict) and msg.get("id") == 1:
                res = msg.get("result")
                return (isinstance(res, dict) and not res.get("down")
                        and res.get("pid") is not None)
        return False
    except (OSError, ValueError):
        return False
    finally:
        s.close()


def lsp_findings(cwd, anchor):
    'Failures recorded by `lspd.py --mcp` (launch) and the canary (semantic).\n\n    A PASS is only evidence while it is fresh. The canary runs every session, so a\n    verdict older than LSP_STALE means the canary stopped running -- and its last\n    ok:true would otherwise keep answering "healthy" for a language server nobody\n    has questioned since.\n\n    And a FAIL is only evidence while it is still true -- see bridge_serving.'
    out = []
    seen = {}
    superseded = set()
    expected = expected_canaries(cwd)
    d = os.path.join(STATE, "lsp")
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            if not name.endswith(".json"):
                continue
            rec = read_json(os.path.join(d, name))
            if not rec:
                continue
            
            
            
            
            
            if os.path.realpath(rec.get("cwd") or "") != os.path.realpath(cwd):
                continue
            server = rec.get("server") or name[:-5]
            
            
            
            
            
            
            
            if rec.get("symbol") and server in expected \
                    and rec["symbol"] != expected[server]:
                superseded.add(server)
                continue
            
            
            
            
            if rec.get("symbol") and server not in expected:
                continue
            seen[server] = rec
            if not rec.get("ok"):
                
                
                
                
                if bridge_serving(cwd, server):
                    continue
                out.append(("LSP %s" % server,
                            rec.get("detail", "failed, with no detail recorded")))
                continue
            age = time.time() - (rec.get("ts") or 0)
            
            
            
            
            
            
            
            
            
            
            
            
            if server not in expected:
                continue
            if age > LSP_STALE:
                out.append(("LSP %s" % server,
                            "last verdict is %d hours old and says PASS: the "
                            "canary is not running, so nothing here has "
                            "been checked since." % int(age // 3600)))

    
    
    for server in sorted(set(expected) - set(seen) - superseded):
        if past_grace(anchor):
            out.append(("LSP %s" % server,
                        "a canary is configured for this project but has NEVER "
                        "written a verdict: the check is absent."))
    return out


SYNC_COMPONENT = "agent-context sync"


def daemon_state_dir():
    'Where the agent-context daemon keeps daemon.info. Mirrors paths.state_dir().'
    if sys.platform == "darwin":
        return os.path.join(HOME, "Library", "Application Support", "agent-context")
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state")
    return os.path.join(base, "agent-context")


def sync_fault_is_over(rec):
    'Has the store synced since the failure this verdict describes?\n\n    A sync failure is a claim about the past, and this is the store-sync twin of\n    bridge_serving above. daemon.note_sync_health() runs on every cycle and clears\n    the verdict on the first healthy one, so the file is live. The hole is the\n    restart. _UNHEALTHY_STREAK is a module global, so a daemon that restarts comes\n    back with the streak zeroed while the last ok:false verdict is still on disk, and\n    nothing rewrites it until that daemon completes a cycle, up to a full sync\n    interval later. A session started inside that window reads a verdict left behind\n    by a process that no longer exists.\n\n    Two independent proofs the fault is over, either sufficient:\n      * the daemon has completed a successful sync since the verdict was written, or\n      * the daemon started after it, so the claim belongs to a dead generation and\n        has not been re-measured by the process running now.\n\n    Fails toward reporting: an unreadable or absent daemon.info means we cannot tell,\n    and an unproven fault is still reported.'
    info = read_json(os.path.join(daemon_state_dir(), "daemon.info"))
    if not info:
        return False
    ts = rec.get("ts") or 0
    return ((info.get("last_successful_sync") or 0) > ts
            or (info.get("started_at") or 0) > ts)


def generic_findings():
    'Anything else that dropped a verdict here (materialize, settings sync, ...).'
    out = []
    if not os.path.isdir(STATE):
        return out
    for name in sorted(os.listdir(STATE)):
        if not name.endswith(".json") or name == "mcp.json":
            continue
        rec = read_json(os.path.join(STATE, name))
        if not rec or rec.get("ok") or hp.stale_daemon_verdict(rec, HOME):
            continue
        if rec.get("component") == SYNC_COMPONENT and sync_fault_is_over(rec):
            continue
        for f in rec.get("failures") or []:
            out.append((rec.get("component", name[:-5]), f))
    return out


def _semver_major(spec):
    'Leading major number of a version or a range, or None when it has no single one.'
    m = re.search(r"(\d+)", str(spec or ""))
    return m.group(1) if m else None


def deps_findings(cwd):
    'A node_modules that no longer matches package.json.\n\n    This is a fails-open fault that fabricates test failures, which is why it gets a\n    SessionStart probe. An install a major version behind package.json renders\n    different DOM and behaves differently, so tests fail on ordinary assertions that\n    read like product defects, and an agent can conclude "main is red" when the\n    install is the cause. A missing dev tool disables the script that calls it with\n    no error.\n\n    Compares only the major, and only for packages that are installed. A minor\n    or patch drift is normal between `npm install` runs and gets no DEGRADED\n    line; a major is a different library. Missing packages are reported separately,\n    because that is the half that disables tooling.'
    pkg_path = os.path.join(cwd, "package.json")
    if not os.path.isfile(pkg_path) or not os.path.isdir(os.path.join(cwd, "node_modules")):
        return []
    pkg = read_json(pkg_path)
    if not pkg:
        return []
    declared = {}
    for section in ("dependencies", "devDependencies"):
        declared.update(pkg.get(section) or {})
    if not declared:
        return []
    drifted, missing = [], []
    for name, spec in sorted(declared.items()):
        if str(spec).startswith(("file:", "link:", "workspace:", "git+", "http")):
            continue                      
        inst = read_json(os.path.join(cwd, "node_modules", *name.split("/"), "package.json"))
        if not inst:
            missing.append(name)
            continue
        want, got = _semver_major(spec), _semver_major(inst.get("version"))
        if want and got and want != got:
            drifted.append(f"{name} {inst.get('version')} installed, {spec} declared")
    out = []
    if drifted:
        out.append(("node_modules",
                    "installed packages are a major version behind package.json: "
                    "%d of %d: %s%s. Tests run against this fail on real "
                    "assertions that read like product defects. Repair: npm ci"
                    % (len(drifted), len(declared), "; ".join(drifted[:3]),
                       "" if len(drifted) <= 3 else f"; +{len(drifted) - 3} more")))
    if missing:
        out.append(("node_modules",
                    "%d declared package(s) are not installed: %s%s. A missing dev tool "
                    "disables the script that calls it without an error. "
                    "Repair: npm ci"
                    % (len(missing), ", ".join(missing[:5]),
                       "" if len(missing) <= 5 else f", +{len(missing) - 5} more")))
    return out


def run_self_heal(findings):
    'Attempt the repairs that are safe to run unattended; return what happened.\n\n    Returns (ran, declined): repairs that were attempted this session,\n    successes and failures alike, and attempts the throttle refused to make.\n\n    The two lists are separate. self-heal.py throttles each fault key for\n    RETRY_AFTER (6h). A throttled result printed under the "Self-repaired at session\n    start" header would tell the session a fault had just been fixed when nothing\n    ran, with the fault\'s own DEGRADED finding two lines below it. A report that\n    contradicts itself teaches the reader to skim the block.\n\n    Only invoked when something is wrong: a healthy machine must\n    not pay for a subprocess every session just to be told it is healthy.'
    if not findings or not os.path.exists(HEAL):
        return [], []
    try:
        proc = subprocess.run([sys.executable, HEAL], capture_output=True,
                              text=True, timeout=HEAL_TIMEOUT)
        report = json.loads(proc.stdout or "{}")
    except subprocess.TimeoutExpired:
        
        
        
        return [], ["self-heal exceeded %ds and was abandoned: nothing below was "
                    "repaired this session." % HEAL_TIMEOUT]
    except (OSError, subprocess.SubprocessError, ValueError):
        return [], []

    ran, declined = [], []
    for r in report.get("results") or []:
        if r.get("skipped"):
            
            
            
            declined.append("%s -- %s" % (r["key"], r["detail"]))
        elif r.get("ok"):
            ran.append("%s: %s" % (r["description"], r["detail"]))
        else:
            ran.append("attempted and failed: %s: %s"
                       % (r["description"], r.get("detail", "no detail")))
    return ran, declined


def push(findings):
    stamp = os.path.join(STATE, ".notify.stamp")
    last = 0
    try:
        with open(stamp) as fh:
            last = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        pass
    if time.time() - last < NOTIFY_THROTTLE or not os.access(NOTIFY, os.X_OK):
        return
    try:
        host = platform.node().split(".")[0]
        body = "; ".join("%s: %s" % (w, d) for w, d in findings[:4])
        subprocess.run([NOTIFY, "-t", "Core systems degraded on %s" % host, body],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        
        
        
        return
    try:
        with open(stamp, "w") as fh:
            fh.write(str(int(time.time())))
    except OSError:
        pass


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        payload = {}
    
    
    
    cwd = main_checkout(payload.get("cwd") or os.getcwd())

    os.makedirs(STATE, exist_ok=True)
    anchor = first_seen()
    spawn_probes(cwd)

    findings = (mcp_findings(anchor) + lsp_findings(cwd, anchor)
                + deps_findings(cwd) + generic_findings())

    
    
    
    
    
    healed, throttled = run_self_heal(findings)
    if healed:
        
        
        
        
        
        
        
        
        findings = (mcp_findings(anchor) + lsp_findings(cwd, anchor)
                    + deps_findings(cwd) + generic_findings())

    if not findings and not healed and not throttled:
        print(json.dumps({"suppressOutput": True}))
        return 0

    lines = []
    if healed:
        
        
        
        lines.append("Self-repaired at session start: %d action(s):" % len(healed))
        lines += ["  - %s" % h for h in healed]
        lines.append("")
    if throttled:
        
        
        lines.append("Repair throttled: %d fault(s) not repaired this session:"
                     % len(throttled))
        lines += ["  - %s" % t for t in throttled]
        lines.append("")
    if findings:
        lines.append("Degraded core systems: %d finding(s) on this machine:"
                     % len(findings))
        lines += ["  - %s: %s" % (what, detail) for what, detail in findings]
        lines.append("")
    lines.append(
        "These systems fail open: the fallback (grep for LSP, no memory, a stale "
        "projection) still produces plausible output.\n"
        "  1. In one line of your reply, name each degraded system and its "
        "repair command. Run a repair only when user asks.\n"
        "  2. Treat a degraded system's answers as unverified: under a degraded LSP, "
        "a negative symbol result is no evidence.\n"
        "  3. Early in a session, ask a 'not found' again: the index may be cold.\n"
        "Details: get_doc(\"core-system-health.md\")."
        if findings else
        "Nothing is degraded now: these repairs succeeded. Mention them once, "
        "briefly, so a recurring fault stays visible."
    )
    context = "\n".join(lines)

    push(findings)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        },
        "systemMessage": (
            "⚠️  %d core system(s) degraded: see the degraded core systems block."
            % len(findings) if findings else
            "🔧  %d core system(s) self-repaired at startup." % len(healed)
        ),
    }))
    return 0


STAMP = os.path.join(STATE, ".preflight.stamp")


def record_run(ok, detail=""):
    'Leave proof on disk that this hook reached the end of its work.\n\n    Read by `preflight-crash-watch`, which runs from home-materialize.py,\n    outside this file, because a watchdog that lives inside the thing it watches\n    reports nothing on the one occasion that matters.'
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(STAMP, "w") as fh:
            json.dump({"ts": int(time.time()), "ok": bool(ok),
                       "detail": detail}, fh)
    except OSError:
        pass


def crash_report(exc):
    'A crash in this hook must not be silent.\n\n    So the hook reports its own death in the one channel that reaches the\n    agent, and exits 0. A non-zero exit here buys nothing (the\n    harness discards it) and risks the block being discarded with it. The\n    report is the product; the exit code is not.\n\n    This covers a crash anywhere in main(). It cannot cover a syntax or import\n    error, because then this function does not exist either -- that case is what\n    the external watchdog is for, and why the two layers are not redundant.'
    tb = traceback.format_exc().strip().splitlines()
    last = tb[-1].strip() if tb else repr(exc)
    where = ""
    for line in reversed(tb):
        if line.strip().startswith("File "):
            where = line.strip()
            break
    detail = ("%s  [%s]" % (last, where)) if where else last
    record_run(False, detail)
    context = (
        "Degraded core systems: 1 finding(s) on this machine:\n"
        "  - preflight-core-health: the health hook itself crashed. %s\n\n"
        "Nothing else was checked (MCP roster, LSP canaries, self-heal). Treat "
        "every core system as unchecked this session; a negative symbol answer "
        "proves nothing.\n"
        "Likely cause: a stale projection (a hook upsert reaches ~/.claude only in "
        "the next session). Run:\n"
        "  python3 ~/.agent-context/global/scripts/home-materialize.py\n"
        "then start a new session." % detail
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        },
        "systemMessage": "⚠️  preflight-core-health crashed: core systems were not checked.",
    }))


if __name__ == "__main__":
    try:
        rc = main()
        record_run(True)
    except Exception as exc:                  
        crash_report(exc)
        rc = 0
    sys.exit(rc)
