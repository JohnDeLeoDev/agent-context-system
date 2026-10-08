#!/usr/bin/env python3
'Battery for round B of store-reader-graph.py (items B1 to B5), written before the code.\n\nAdditive to the four locked batteries. B1 known_gaps_extra, notice and blind spots.\nB2 text mode and --help. B3 --out path refusal. B4 atomic --out. B5 report-only.\nFixtures live under ~/.cache/tmp and are removed at the end. stdlib only.'

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable

try:
    import resource
except ImportError:  
    resource = None

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "store-reader-graph.py")
CLASSES = ("hooks", "scripts", "docs", "skills", "commands", "agents", "manifests")
ALL_CHECKED = ["--checked", "m4", "--checked", "rp", "--checked", "pc",
               "--checked", "mirror-a", "--checked", "mirror-b"]
EXTRA_KEYWORDS = ("xargs", ".git/hooks", "-m", "plugins", "system")
EXTRA_BLIND_SPOTS = [
    "lists read via xargs (.txt, .md, .rst, .jsonl)",
    ".git/hooks in the store",
    "python3 -m and __import__ references",
    "~/.claude/plugins and other ~/.claude subdirectories",
    "/etc/systemd/system and /etc/cron.d",
    "per-repo .claude/settings.json outside the store",
    "~/.config/fish, autostart and environment.d",
]
NOTICE_EXTRA_PHRASE = "known_gaps and known_gaps_extra are not exhaustive"

scratch_root = os.path.join(os.path.expanduser("~"), ".cache", "tmp")
os.makedirs(scratch_root, exist_ok=True)
tmp = tempfile.mkdtemp(prefix="store-reader-graph-round-b-test-", dir=scratch_root)

passed = 0
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print("  PASS " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + ("  -- " + detail if detail else ""))


def write(root: str, rel: str, content: str) -> str:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def build(name: str, files: dict[str, str]) -> str:
    root = os.path.join(tmp, name)
    for rel, content in files.items():
        write(root, rel, content)
    for sub in ("store", "home", "outdir"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root


def run_tool(args: list[str], env_overrides: dict[str, str] | None = None,
             preexec: Callable[[], None] | None = None) -> tuple[int, str, str]:
    env = dict(os.environ)
    env.pop("AGENT_CONTEXT_STORE", None)
    env.update(env_overrides or {})
    proc = subprocess.run([sys.executable, TOOL] + args, capture_output=True, text=True,
                          env=env, timeout=120, preexec_fn=preexec)
    return proc.returncode, proc.stdout, proc.stderr


def base_args(root: str) -> list[str]:
    return ["--store", os.path.join(root, "store"), "--home", os.path.join(root, "home")]


def run_json(root: str, extra: list[str] | None = None) -> tuple[int, Any, str]:
    rc, out, err = run_tool(base_args(root) + ["--json"] + (extra or []), {"HOME": root})
    try:
        return rc, json.loads(out), err
    except ValueError:
        return rc, None, err + out[:300]


def run_out(root: str, out_path: str, extra: list[str] | None = None,
            preexec: Callable[[], None] | None = None) -> tuple[int, str, str]:
    return run_tool(base_args(root) + ["--json", "--out", out_path] + (extra or []),
                    {"HOME": root}, preexec)


def key_entry(report: Any, cls: str, key: str) -> dict[str, Any]:
    section = report.get("classes", {}).get(cls, {}) if isinstance(report, dict) else {}
    found = section.get("keys", {}).get(key) if isinstance(section, dict) else None
    return found if isinstance(found, dict) else {}


def one_line(err: str) -> bool:
    return len(err.strip().splitlines()) == 1 and "Traceback" not in err


def snapshot(*roots: str) -> dict[str, str]:
    state: dict[str, str] = {}
    for root in roots:
        for directory, dirs, names in os.walk(root):
            for name in dirs + names:
                path = os.path.join(directory, name)
                rel = os.path.relpath(path, os.path.dirname(root))
                if os.path.islink(path):
                    state[rel] = "link:" + os.readlink(path)
                elif os.path.isfile(path):
                    with open(path, "rb") as handle:
                        state[rel] = hashlib.sha256(handle.read()).hexdigest()
                else:
                    state[rel] = "dir"
    return state


def runs(*names: str) -> str:
    return "import subprocess\n" + "".join(
        'subprocess.run(["python3", "%s"])\n' % name for name in names)


def settings_json(hook_name: str) -> str:
    command = "python3 $HOME/.agent-context/global/hooks/" + hook_name
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": command}]}]}}, indent=2) + "\n"


G = "store/global/"
FIXTURE_FILES = {
    G + "hooks/hook-live.py": "print('live')\n",
    "home/.claude/settings.json": settings_json("hook-live.py"),
    G + "scripts/test-t1.py": runs("script-t1.py"),
    G + "scripts/script-t1.py": "print('t1')\n",
    G + "scripts/script-orphan.py": "print('orphan')\n",
}
ORPHAN_KEY = "global/scripts/script-orphan.py"

out_counter = 0


def fresh_out(root: str) -> str:
    global out_counter
    out_counter += 1
    return os.path.join(root, "outdir", "fresh-%d.json" % out_counter)


def read_json_file(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def refused(root: str, label: str, target: str) -> None:
    rc, _o, err = run_out(root, target)
    check("--out %s: exit 2" % label, rc == 2, "rc=%s err=%s" % (rc, err.strip()[:120]))
    check("--out %s: one stderr line naming --out, no traceback" % label,
          one_line(err) and "--out" in err, err[:200])
    check("--out %s: nothing created" % label, not os.path.lexists(target))


try:
    root = build("main", FIXTURE_FILES)

    print("fixture sanity")
    rc, base, err = run_json(root)
    check("default --json run exits 0 and prints a JSON object",
          rc == 0 and isinstance(base, dict), "rc=%s %s" % (rc, err[:200]))
    check("fixture: script-orphan.py is no-reader-found with a caveat",
          key_entry(base, "scripts", ORPHAN_KEY).get("verdict") == "no-reader-found"
          and bool(key_entry(base, "scripts", ORPHAN_KEY).get("caveat")))
    check("known_gaps is still exactly G1..G11",
          isinstance(base, dict) and [g.get("id") for g in base.get("known_gaps", [])]
          == ["G%d" % n for n in range(1, 12)])

    variants: list[tuple[str, list[str]]] = [("default", []), ("all checked", ALL_CHECKED)]
    variants += [("--class " + cls, ["--class", cls]) for cls in CLASSES]
    reports: dict[str, Any] = {}
    for label, extra in variants:
        rc, report, err = run_json(root, extra)
        reports[label] = report
        check("%s: --json run exits 0 and prints a JSON object" % label,
              rc == 0 and isinstance(report, dict), "rc=%s %s" % (rc, err[:200]))

    print("B1: known_gaps_extra, notice, blind spots")
    for label, report in reports.items():
        extra_gaps = report.get("known_gaps_extra") if isinstance(report, dict) else None
        shape_ok = (isinstance(extra_gaps, list) and len(extra_gaps) == 5
                    and all(isinstance(g, dict) and set(g) == {"id", "summary"}
                            for g in extra_gaps))
        check("%s: known_gaps_extra is 5 objects with exactly id and summary" % label,
              shape_ok, "got %s" % str(extra_gaps)[:200])
        ids = [g.get("id") for g in extra_gaps] if shape_ok and isinstance(extra_gaps, list) \
            else []
        check("%s: known_gaps_extra ids are G12..G16 in order" % label,
              ids == ["G%d" % n for n in range(12, 17)], "got %s" % ids)
        check("%s: every extra summary is a non-empty string" % label,
              shape_ok and isinstance(extra_gaps, list)
              and all(isinstance(g["summary"], str) and g["summary"].strip()
                      for g in extra_gaps))
        notice = report.get("notice") if isinstance(report, dict) else None
        check("%s: notice contains %r" % (label, NOTICE_EXTRA_PHRASE),
              isinstance(notice, str) and NOTICE_EXTRA_PHRASE in notice, "got %r" % (notice,))
        spots = report.get("scan_blind_spots") if isinstance(report, dict) else None
        missing = [s for s in EXTRA_BLIND_SPOTS if not isinstance(spots, list) or s not in spots]
        check("%s: scan_blind_spots contains the seven round-B entries" % label,
              not missing, "missing %s" % missing)
        old_gaps = report.get("known_gaps") if isinstance(report, dict) else None
        check("%s: known_gaps stays 11 objects" % label,
              isinstance(old_gaps, list) and len(old_gaps) == 11)
    extras_default = base.get("known_gaps_extra") if isinstance(base, dict) else None
    for n, keyword in enumerate(EXTRA_KEYWORDS):
        summary = ""
        if isinstance(extras_default, list) and len(extras_default) > n \
                and isinstance(extras_default[n], dict):
            summary = str(extras_default[n].get("summary", ""))
        check("G%d summary contains %r" % (n + 12, keyword),
              keyword.lower() in summary.lower(), "summary %r" % summary[:120])
    for label, extra in (("default", []), ("all checked", ALL_CHECKED),
                         ("--class docs", ["--class", "docs"])):
        target = fresh_out(root)
        rc, stdout, _e = run_out(root, target, extra)
        written = read_json_file(target)
        try:
            printed: Any = json.loads(stdout)
        except ValueError:
            printed = None
        check("%s: --out exits 0, file equals stdout and has known_gaps_extra" % label,
              rc == 0 and isinstance(written, dict) and written == printed
              and isinstance(written.get("known_gaps_extra"), list)
              and len(written["known_gaps_extra"]) == 5, "rc=%s" % rc)

    print("B2: text mode and --help")
    rc_text, text, err_text = run_tool(base_args(root), {"HOME": root})
    check("text run exits 0", rc_text == 0, "rc=%s %s" % (rc_text, err_text[:200]))
    lines = text.splitlines()
    more_line = next((ln for ln in lines if ln.startswith("known gaps (more):")), "")
    check("a line 'known gaps (more): 5 G12 G13 G14 G15 G16'",
          more_line.split() == ["known", "gaps", "(more):", "5", "G12", "G13", "G14", "G15",
                                "G16"], "line %r" % more_line[:200])
    check("locked line 'known gaps: 11 G1 ... G11' is still there",
          any(ln.split() == ["known", "gaps:", "11"] + ["G%d" % n for n in range(1, 12)]
              for ln in lines))
    for n in range(12, 17):
        summary = ""
        if isinstance(extras_default, list) and len(extras_default) > n - 12 \
                and isinstance(extras_default[n - 12], dict):
            summary = str(extras_default[n - 12].get("summary", ""))
        check("a line '  G%d: <summary>'" % n,
              bool(summary) and ("  G%d: %s" % (n, summary)) in lines,
              "summary %r" % summary[:80])
    no_reader_keys = [(cls, key) for cls in CLASSES for key, info in
                      (base.get("classes", {}).get(cls, {}).get("keys", {}).items()
                       if isinstance(base, dict) else [])
                      if info.get("verdict") == "no-reader-found" and info.get("caveat")]
    check("fixture: at least one no-reader-found key with a caveat", bool(no_reader_keys))
    for cls, key in no_reader_keys:
        info = key_entry(base, cls, key)
        key_re = re.compile(r"^\s+no-reader-found\s+" + re.escape(key) + r"(\s|$)")
        at = next((i for i, ln in enumerate(lines) if key_re.match(ln)), -1)
        window = "\n".join(lines[at:at + 2]) if at >= 0 else ""
        check("%s: no-reader-found line is found in text mode" % key, at >= 0)
        check("%s: no-reader-found line or next carries its caveat" % key,
              at >= 0 and str(info.get("caveat")) in window, "window %r" % window[:300])
    for flag in ("--help", "-h"):
        rc_help, help_out, _e = run_tool([flag])
        check("%s exits 0 and says 'keep-list evidence only'" % flag,
              rc_help == 0 and "keep-list evidence only" in help_out,
              "rc=%s out %r" % (rc_help, help_out[:120]))

    print("B3: --out refusal by location")
    b3 = build("b3", FIXTURE_FILES)
    denied_missing_parent = (
        ".aws/x.json", ".gnupg/x.json", ".pi/agent/rep.json", ".local/share/chezmoi/x.json",
        ".npm/x.json", ".mozilla/x.json", ".local/bin/x.json", ".local/other/x.json",
        ".local/x.json", ".CLAUDE/x.json", "LIBRARY/x.json", "library/x.json",
        ".Aws/x.json", ".hidden-report.json",
    )
    for rel in denied_missing_parent:
        refused(b3, "$HOME/%s (parent directory missing)" % rel, os.path.join(b3, rel))
    for rel in (".aws", ".gnupg", ".pi/agent", ".local/share/chezmoi", ".npm"):
        os.makedirs(os.path.join(b3, rel), exist_ok=True)
        refused(b3, "$HOME/%s/x.json (parent exists)" % rel, os.path.join(b3, rel, "x.json"))
    refused(b3, "inside the store", os.path.join(b3, "store", "global", "new-report.json"))
    scan_roots = (".local/bin", "Library/LaunchAgents", ".config/systemd/user", ".claude",
                  ".codex", ".copilot", ".config/opencode", ".pi/agent",
                  ".pi/agent/extensions/sub")
    for rel in scan_roots:
        os.makedirs(os.path.join(b3, "home", rel), exist_ok=True)
        refused(b3, "--home/%s (scanned root)" % rel, os.path.join(b3, "home", rel, "x.json"))
    for rel in (".cache/reports", ".local/state/reports", ".local/state"):
        os.makedirs(os.path.join(b3, rel), exist_ok=True)
    accepted = (("$HOME/outdir", os.path.join(b3, "outdir", "ok.json")),
                ("$HOME/.cache/reports", os.path.join(b3, ".cache", "reports", "ok.json")),
                ("$HOME/.local/state/reports",
                 os.path.join(b3, ".local", "state", "reports", "ok.json")),
                ("$HOME/.local/state", os.path.join(b3, ".local", "state", "ok.json")))
    for label, target in accepted:
        rc, _o, err = run_out(b3, target)
        check("--out fresh path under %s: accepted with exit 0" % label,
              rc == 0 and os.path.isfile(target), "rc=%s %s" % (rc, err[:200]))
    slash_dir = os.path.join(b3, "outdir", "newdir") + os.sep
    refused(b3, "path ending with a slash", slash_dir)
    refused(b3, "fresh file name ending with a slash",
            os.path.join(b3, "outdir", "fresh-name.json") + os.sep)
    rc, _o, err = run_out(b3, os.path.join(b3, "outdir"))
    check("--out existing directory: exit 2, one stderr line",
          rc == 2 and one_line(err), "rc=%s %s" % (rc, err[:120]))
    fifo = os.path.join(b3, "outdir", "pipe.json")
    os.mkfifo(fifo)
    rc, _o, err = run_out(b3, fifo)
    check("--out FIFO: exit 2, one stderr line, still a FIFO",
          rc == 2 and one_line(err) and stat.S_ISFIFO(os.lstat(fifo).st_mode),
          "rc=%s %s" % (rc, err[:120]))

    print("B4: atomic --out")
    b4 = build("b4", FIXTURE_FILES)
    ok_dir = os.path.join(b4, "outdir", "ok")
    os.makedirs(ok_dir)
    ok_target = os.path.join(ok_dir, "rep.json")
    rc, _o, err = run_out(b4, ok_target)
    mode = stat.S_IMODE(os.stat(ok_target).st_mode) if os.path.isfile(ok_target) else None
    check("success: exit 0, valid JSON, mode 0600, no temp file left",
          rc == 0 and isinstance(read_json_file(ok_target), dict) and mode == 0o600
          and os.listdir(ok_dir) == ["rep.json"],
          "rc=%s mode=%s files=%s %s" % (rc, oct(mode) if mode is not None else None,
                                         os.listdir(ok_dir), err[:120]))

    def limit_file_size(limit: int) -> Callable[[], None]:
        def apply() -> None:
            if resource is not None:
                resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
        return apply

    rlimit_works = False
    if resource is not None:
        probe = os.path.join(b4, "outdir", "probe.bin")
        code = "with open(%r, 'wb') as h:\n    h.write(b'x' * 5000)\n" % probe
        probe_run = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                   timeout=60, preexec_fn=limit_file_size(1000))
        rlimit_works = probe_run.returncode != 0
    if rlimit_works:
        for limit in (1, 1000):
            lim_dir = os.path.join(b4, "outdir", "lim%d" % limit)
            os.makedirs(lim_dir)
            lim_target = os.path.join(lim_dir, "rep.json")
            rc, _o, err = run_out(b4, lim_target, preexec=limit_file_size(limit))
            check("RLIMIT_FSIZE %d: exit nonzero, one stderr line, no traceback" % limit,
                  rc != 0 and one_line(err), "rc=%s %s" % (rc, err[:200]))
            check("RLIMIT_FSIZE %d: final target does not exist" % limit,
                  not os.path.lexists(lim_target))
            check("RLIMIT_FSIZE %d: no temp file left in the directory" % limit,
                  os.listdir(lim_dir) == [], "left %s" % os.listdir(lim_dir))
    else:
        print("  SKIP RLIMIT_FSIZE checks: platform cannot enforce it")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("  SKIP read-only directory checks: running as root")
    else:
        ro_dir = os.path.join(b4, "outdir", "ro")
        os.makedirs(ro_dir)
        os.chmod(ro_dir, 0o500)
        try:
            rc, _o, err = run_out(b4, os.path.join(ro_dir, "rep.json"))
        finally:
            os.chmod(ro_dir, 0o700)
        check("read-only directory: exit nonzero, one stderr line, no traceback",
              rc != 0 and one_line(err), "rc=%s %s" % (rc, err[:200]))
        check("read-only directory: target absent and no temp file left",
              os.listdir(ro_dir) == [], "left %s" % os.listdir(ro_dir))
    for attempt in range(5):
        race_dir = os.path.join(b4, "outdir", "race%d" % attempt)
        os.makedirs(race_dir)
        race_target = os.path.join(race_dir, "rep.json")
        env = dict(os.environ, HOME=b4)
        env.pop("AGENT_CONTEXT_STORE", None)
        command = [sys.executable, TOOL] + base_args(b4) + ["--json", "--out", race_target]
        procs = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, env=env) for _ in range(2)]
        results: list[tuple[int, str]] = []
        try:
            for proc in procs:
                _out, proc_err = proc.communicate(timeout=120)
                results.append((proc.returncode, proc_err))
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
        codes = sorted(code for code, _e in results)
        check("concurrent run %d: exactly one exits 0 and one nonzero" % attempt,
              len(results) == 2 and codes[0] == 0 and codes[1] != 0, "codes %s" % codes)
        check("concurrent run %d: loser prints one stderr line, no traceback" % attempt,
              all(one_line(e) for c, e in results if c != 0)
              and all(not e.strip() for c, e in results if c == 0), str(results)[:300])
        check("concurrent run %d: target is one complete JSON document, no temp left" % attempt,
              isinstance(read_json_file(race_target), dict)
              and os.listdir(race_dir) == ["rep.json"], "files %s" % os.listdir(race_dir))

    print("B5: report-only")
    b5 = build("b5", FIXTURE_FILES)
    before = snapshot(os.path.join(b5, "store"), os.path.join(b5, "home"))
    rc_a, _o, _e = run_out(b5, fresh_out(b5))
    rc_b, _o, _e = run_tool(base_args(b5), {"HOME": b5})
    rc_c, _o, _e = run_tool(base_args(b5) + ["--json", "--class", "hooks"] + ALL_CHECKED,
                            {"HOME": b5})
    after = snapshot(os.path.join(b5, "store"), os.path.join(b5, "home"))
    check("fixture store and home are byte-identical after runs incl. --out",
          rc_a == 0 and rc_b == 0 and rc_c == 0 and before == after,
          "rc=%s/%s/%s changed: %s" % (rc_a, rc_b, rc_c, sorted(
              k for k in set(before) | set(after) if before.get(k) != after.get(k))[:3]))
finally:
    for base_dir, dirs, _names in os.walk(tmp):
        for name in dirs:
            os.chmod(os.path.join(base_dir, name), 0o700)
    shutil.rmtree(tmp, ignore_errors=True)

total = passed + len(failures)
print("test-store-reader-graph-round-b: %d/%d passed" % (passed, total))
sys.exit(1 if failures else 0)
