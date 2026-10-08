#!/usr/bin/env python3
'Battery for criterion R7 of store-reader-graph.py: verdicts by reachability from roots.\n\nAdditive to test-store-reader-graph.py (locked): every old field and verdict stays, this\nfile checks the new fields (`via`, `reach`, `chain`, `loader_lines`, `unchecked_roots`)\nagainst a fixture with a known graph. Fixtures live under ~/.cache/tmp. stdlib only.'

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "store-reader-graph.py")
CLASSES = ("hooks", "scripts", "docs", "skills", "commands", "agents", "manifests")
REACH_VALUES = ("root-reachable", "test-only", "unreachable", "uncertain")
VIA_ROOTS = ("root:settings", "root:manifest", "root:harness-config", "root:launcher",
             "root:unit", "root:command-shell")
LAPTOP_ROOT = "laptop launchers and units (not inspected)"
DEFAULT_ROOTS = ["m4", "pc", "rp", "mirror-a", "mirror-b", LAPTOP_ROOT]
DEFAULT_UNCHECKED = ["m4", "rp", "pc", "mirror-a", "mirror-b"]
ALL_CHECKED = ["--checked", "m4", "--checked", "rp", "--checked", "pc",
               "--checked", "mirror-a", "--checked", "mirror-b"]

scratch_root = os.path.join(os.path.expanduser("~"), ".cache", "tmp")
os.makedirs(scratch_root, exist_ok=True)
tmp = tempfile.mkdtemp(prefix="store-reader-graph-reach-test-", dir=scratch_root)

passed = 0
failures = []


def check(label, ok, detail=""):
    global passed
    if ok:
        passed += 1
        print("  PASS " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + ("  -- " + detail if detail else ""))


def write(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content.encode())
    return path


def runs(*names):
    'Python source whose non-comment lines each run one named file.'
    return "import subprocess\n" + "".join(
        'subprocess.run(["python3", "%s"])\n' % name for name in names)


def line_of(content, needle):
    return next(i for i, ln in enumerate(content.splitlines(), 1) if needle in ln)


def settings_json(commands):
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": c} for c in commands]}]}}, indent=2) + "\n"


def hook_command(name):
    return "python3 $HOME/.agent-context/global/hooks/" + name


def script_command(name):
    return "python3 $HOME/.agent-context/global/scripts/" + name


GLOB_LOADER = (
    "import glob, os\n"
    "ROOT = os.path.expanduser(\"~/.agent-context/global\")\n"
    "for path in glob.glob(os.path.join(ROOT, \"hooks/*.py\")):\n"
    "    pass\n"
)
FSTRING_LOADER = (
    "import subprocess\n"
    "SCRIPTS = \"/x\"\n"
    "for name in (\"a\",):\n"
    "    subprocess.run([\"bash\", f\"{SCRIPTS}/{name}.sh\"])\n"
)
G = "store/global/"
FILES = {
    
    G + "hooks/hook-h1.py": runs("script-s1.py"),
    G + "scripts/script-s1.py": runs("script-s2.py"),
    G + "scripts/script-s2.py": "print('s2')\n",
    
    G + "scripts/test-s3.py": runs("script-s3.py", "hook-behind-test.py"),
    G + "scripts/script-s3.py": runs("script-s3-child.py"),
    G + "scripts/script-s3-child.py": "print('child')\n",
    G + "hooks/hook-behind-test.py": "print('behind test')\n",
    
    G + "scripts/hook-test-cases.py": runs("script-behind-hook-test.py"),
    G + "scripts/script-behind-hook-test.py": "print('x')\n",
    G + "scripts/test_under.py": runs("script-behind-test-under.py"),
    G + "scripts/script-behind-test-under.py": "print('x')\n",
    
    G + "scripts/script-s4.py": "print('s4')\n",
    G + "scripts/script-orphan.py": "print('nobody')\n",
    
    G + "scripts/script-s5.py": runs("script-s6.py"),
    G + "scripts/script-s6.py": runs("script-s5.py"),
    
    G + "hooks/hook-h2.py": runs("script-s7.py"),
    G + "scripts/script-s7.py": runs("script-s8.py"),
    G + "scripts/script-s8.py": runs("script-s7.py"),
    
    G + "hooks/hook-d0.py": runs("script-dleft.py", "script-dright.py"),
    G + "scripts/script-dleft.py": runs("script-dbottom.py"),
    G + "scripts/script-dright.py": runs("script-dmid.py"),
    G + "scripts/script-dmid.py": runs("script-dbottom.py"),
    G + "scripts/script-dbottom.py": runs("script-dtail.py"),
    G + "scripts/script-dtail.py": "print('tail')\n",
    
    G + "scripts/run-tests.py": runs("test-wired.py"),
    G + "scripts/test-wired.py": runs("script-behind-test.py"),
    G + "scripts/script-behind-test.py": "print('x')\n",
    
    G + "scripts/unreachable-loader.py": GLOB_LOADER,
    G + "hooks/hook-h9.py": "print('h9')\n",
    G + "hooks/hook-loader.py": FSTRING_LOADER,
    G + "scripts/dyn-target.sh": "echo target\n",
    
    G + "commands/cmd-d1.md": (
        "Run the check.\n\n```bash\n"
        "cat \"$HOME/.agent-context/shared-docs/doc-d1.md\"\n```\n"),
    G + "hooks/hook-doc-reader.py": (
        "print(open(os.path.expanduser(\"~/.agent-context/shared-docs/doc-d2.md\")).read())\n"),
    G + "scripts/script-doc3-reader.py": (
        "print(open(os.path.expanduser(\"~/.agent-context/shared-docs/doc-d3.md\")).read())\n"),
    "store/shared-docs/doc-d1.md": "# D1\n",
    "store/shared-docs/doc-d2.md": "# D2\n",
    "store/shared-docs/doc-d3.md": "# D3\n",
    "store/shared-docs/doc-d4.md": "# D4\n",
    "store/shared-docs/guide.md": (
        "# Guide\n\nThe script script-s4.py is described here, and so is doc-d4.md.\n"),
    
    G + "hooks/hook-manifest.py": "print('m')\n",
    G + "scripts/script-mcp.py": "print('mcp')\n",
    G + "hooks-manifest.json": (
        "{\n  \"hooks\": {\n    \"hook-manifest.py\": {\"sha256\": \"abc\"}\n  }\n}\n"),
    G + "mcp-servers.json": (
        "{\n  \"servers\": {\n    \"demo\": {\n      \"command\": \"python3\",\n"
        "      \"args\": [\"/home/x/.agent-context/global/scripts/script-mcp.py\"]\n"
        "    }\n  }\n}\n"),
    G + "hooks/hook-codex.py": "print('c')\n",
    G + "hooks/hook-opencode.py": "print('o')\n",
    G + "hooks/hook-copilot.py": "print('p')\n",
    G + "hooks/hook-pi.py": "print('pi')\n",
    G + "scripts/script-launcher.py": "print('l')\n",
    G + "scripts/script-plist.py": "print('p')\n",
    G + "scripts/script-unit.py": "print('u')\n",
    "home/.claude/settings.json": settings_json([
        hook_command("hook-h1.py"), hook_command("hook-h2.py"), hook_command("hook-d0.py"),
        hook_command("hook-doc-reader.py"), hook_command("hook-loader.py"),
        script_command("run-tests.py")]),
    "home/.codex/hooks.json": (
        "{\n  \"hooks\": {\n    \"PreToolUse\": [{\"command\": "
        "\"python3 ~/.agent-context/global/hooks/hook-codex.py\"}]\n  }\n}\n"),
    "home/.config/opencode/opencode.json": (
        "{\n  \"hook\": \"python3 ~/.agent-context/global/hooks/hook-opencode.py\"\n}\n"),
    "home/.copilot/config.json": (
        "{\n  \"hook\": \"python3 ~/.agent-context/global/hooks/hook-copilot.py\"\n}\n"),
    "home/.pi/agent/extensions/ext.ts": (
        "const cmd = \"python3 ~/.agent-context/global/hooks/hook-pi.py\";\n"),
    "home/.local/bin/mylauncher": (
        "#!/bin/sh\nexec python3 \"$HOME/.agent-context/global/scripts/"
        "script-launcher.py\" \"$@\"\n"),
    "home/Library/LaunchAgents/x.plist": (
        "<plist><dict><key>ProgramArguments</key><array>"
        "<string>/Users/x/.agent-context/global/scripts/script-plist.py</string>"
        "</array></dict></plist>\n"),
    "home/.config/systemd/user/x.service": (
        "[Service]\nExecStart=/usr/bin/python3 %h/.agent-context/global/scripts/"
        "script-unit.py\n"),
}


def snapshot(*roots):
    state = {}
    for root in roots:
        for base, _dirs, names in os.walk(root):
            for name in names:
                path = os.path.join(base, name)
                with open(path, "rb") as handle:
                    state[path] = hashlib.sha256(handle.read()).hexdigest()
    return state


def run_tool(args, env_overrides=None):
    env = dict(os.environ)
    env.pop("AGENT_CONTEXT_STORE", None)
    env.update(env_overrides or {})
    proc = subprocess.run([sys.executable, TOOL] + args, capture_output=True,
                          text=True, env=env, timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


def run_json(root, extra=None):
    args = ["--store", os.path.join(root, "store"), "--home", os.path.join(root, "home"),
            "--json"] + (extra or [])
    rc, out, err = run_tool(args, {"HOME": root})
    try:
        return rc, json.loads(out), err
    except ValueError:
        return rc, None, err + out[:300]


def entry(report, cls, key):
    if not isinstance(report, dict):
        return None
    section = (report.get("classes") or {}).get(cls)
    keys = section.get("keys") if isinstance(section, dict) else None
    found = keys.get(key) if isinstance(keys, dict) else None
    return found if isinstance(found, dict) else None


def reach_is(report, cls, key, expected):
    info = entry(report, cls, key)
    got = info.get("reach") if info else None
    return got == expected, "reach %r, want %r" % (got, expected)


def chain_of(report, cls, key):
    info = entry(report, cls, key)
    chain = info.get("chain") if info else None
    if not isinstance(chain, list):
        return None
    return [c for c in chain if isinstance(c, dict)]


def chain_ok(report, cls, key, length, first_suffix, first_via, last_suffix=None):
    "A root-to-key chain: every step is a reader, each step's file is named by the last."
    chain = chain_of(report, cls, key)
    if chain is None:
        return False, "no chain list"
    if len(chain) != length:
        return False, "chain length %d, want %d: %s" % (len(chain), length, str(chain)[:200])
    for step in chain:
        if not (isinstance(step.get("file"), str) and isinstance(step.get("line"), int)
                and step["line"] >= 1 and isinstance(step.get("snippet"), str)
                and isinstance(step.get("via"), str)):
            return False, "malformed step %s" % step
    if not chain[0]["file"].endswith(first_suffix) or chain[0]["via"] != first_via:
        return False, "first step %s, want %s via %s" % (chain[0], first_suffix, first_via)
    if any(step["via"] != "script" for step in chain[1:]):
        return False, "later steps must be via script: %s" % chain[1:]
    for before, after in zip(chain, chain[1:]):
        if os.path.basename(after["file"]) not in before["snippet"]:
            return False, "%s not named in %r" % (after["file"], before["snippet"])
    if os.path.basename(key) not in chain[-1]["snippet"]:
        return False, "last snippet %r does not name %s" % (chain[-1]["snippet"], key)
    if last_suffix and not chain[-1]["file"].endswith(last_suffix):
        return False, "last step in %s, want %s" % (chain[-1]["file"], last_suffix)
    return True, ""


def exec_vias(report, cls, key, reader_suffix):
    info = entry(report, cls, key)
    readers = info.get("readers") if info else None
    if not isinstance(readers, list):
        return []
    return [r.get("via") for r in readers if isinstance(r, dict) and r.get("kind") == "exec"
            and str(r.get("file", "")).endswith(reader_suffix)]


def has_caveat_naming(info, roots):
    caveat = info.get("caveat") if info else None
    return (isinstance(caveat, str) and bool(caveat.strip()) and bool(roots)
            and all(root in caveat for root in roots))


try:
    root = os.path.join(tmp, "main")
    for rel, content in FILES.items():
        write(root, rel, content)
    os.makedirs(os.path.join(root, "outdir"), exist_ok=True)

    print("fixture sanity")
    check("tool exists: global/scripts/store-reader-graph.py", os.path.isfile(TOOL), TOOL)
    check("fixture: hooks, scripts, docs, commands and home roots exist",
          all(os.path.isfile(os.path.join(root, rel)) for rel in (
              G + "hooks/hook-h1.py", G + "scripts/test-s3.py", G + "scripts/script-s5.py",
              "store/shared-docs/doc-d1.md", G + "commands/cmd-d1.md",
              "home/.claude/settings.json", "home/.local/bin/mylauncher")))
    with open(os.path.join(root, "home/.claude/settings.json"), encoding="utf-8") as handle:
        settings_text = handle.read()
    check("fixture: settings.json parses and names six commands, one per line",
          len(json.loads(settings_text)["hooks"]["PreToolUse"][0]["hooks"]) == 6
          and all(sum(1 for ln in settings_text.splitlines() if name in ln) == 1
                  for name in ("hook-h1.py", "hook-h2.py", "hook-d0.py", "run-tests.py")))
    check("fixture: no global/docs directory (docs fall back to shared-docs)",
          not os.path.exists(os.path.join(root, "store/global/docs")))

    before = snapshot(os.path.join(root, "store"), os.path.join(root, "home"))
    rc, report, err = run_json(root)
    check("--json run exits 0 and prints JSON", rc == 0 and report is not None,
          "rc=%s %s" % (rc, err[:200]))
    ok, detail = (entry(report, "hooks", "global/hooks/hook-h1.py") is not None, "no entry")
    check("fixture: hook-h1.py is a hooks key", ok, detail)

    print("R7.9-style: old fields stay")
    for cls, key, verdict in (
            ("scripts", "global/scripts/script-s1.py", "needed-on-disk"),
            ("scripts", "global/scripts/script-s4.py", "mention-only"),
            ("scripts", "global/scripts/script-orphan.py", "no-reader-found"),
            ("hooks", "global/hooks/hook-h9.py", "uncertain"),
            ("docs", "shared-docs/doc-d1.md", "needed-on-disk")):
        info = entry(report, cls, key)
        check("%s keeps verdict %s" % (key, verdict),
              bool(info) and info.get("verdict") == verdict and "reader_count" in info
              and "exec_count" in info and "uncertain_via" in info,
              str(info.get("verdict") if info else None))
    check("unchecked_machines keeps its default list",
          isinstance(report, dict) and report.get("unchecked_machines") == DEFAULT_UNCHECKED)

    print("R7.1: via on every reader")
    via_cases = (
        ("hooks", "global/hooks/hook-h1.py", ".claude/settings.json", "root:settings"),
        ("hooks", "global/hooks/hook-manifest.py", "hooks-manifest.json", "root:manifest"),
        ("scripts", "global/scripts/script-mcp.py", "mcp-servers.json", "root:manifest"),
        ("hooks", "global/hooks/hook-codex.py", ".codex/hooks.json", "root:harness-config"),
        ("hooks", "global/hooks/hook-opencode.py", "opencode.json", "root:harness-config"),
        ("hooks", "global/hooks/hook-copilot.py", ".copilot/config.json",
         "root:harness-config"),
        ("hooks", "global/hooks/hook-pi.py", "ext.ts", "root:harness-config"),
        ("scripts", "global/scripts/script-launcher.py", "mylauncher", "root:launcher"),
        ("scripts", "global/scripts/script-plist.py", "x.plist", "root:unit"),
        ("scripts", "global/scripts/script-unit.py", "x.service", "root:unit"),
        ("docs", "shared-docs/doc-d1.md", "cmd-d1.md", "root:command-shell"),
        ("scripts", "global/scripts/script-s1.py", "hook-h1.py", "script"),
        ("scripts", "global/scripts/script-s2.py", "script-s1.py", "script"),
    )
    for cls, key, suffix, want in via_cases:
        got = exec_vias(report, cls, key, suffix)
        check("%s: exec reader in %s has via %s" % (os.path.basename(key), suffix, want),
              bool(got) and all(v == want for v in got), "via %r" % (got,))
    all_readers = [r for cls in CLASSES
                   for info in ((report or {}).get("classes", {}).get(cls, {})
                                .get("keys", {}).values())
                   for r in info.get("readers", [])]
    check("every reader in the report has a string via",
          bool(all_readers) and all(isinstance(r.get("via"), str) and r["via"]
                                    for r in all_readers),
          str([r for r in all_readers if not r.get("via")][:2]))
    check("every exec reader's via is a root:* value or script",
          bool(all_readers) and all(r.get("via") in VIA_ROOTS + ("script",)
                                    for r in all_readers if r.get("kind") == "exec"),
          str([r.get("via") for r in all_readers if r.get("kind") == "exec"
               and r.get("via") not in VIA_ROOTS + ("script",)][:3]))
    check("existing kind values stay on readers",
          bool(all_readers) and all(r.get("kind") in ("exec", "mention")
                                    for r in all_readers))

    print("R7.2: reach and chain on every key")
    sections = ((report or {}).get("classes") or {})
    every_entry = [info for cls in CLASSES for info in sections.get(cls, {})
                   .get("keys", {}).values()]
    check("every key has reach in the four values",
          bool(every_entry) and all(e.get("reach") in REACH_VALUES for e in every_entry),
          str([e.get("reach") for e in every_entry][:3]))
    check("every key has a chain list",
          bool(every_entry) and all(isinstance(e.get("chain"), list) for e in every_entry))

    print("R7.2/R7.3: root-reachable and chains")
    chain_cases = (
        ("hooks", "global/hooks/hook-h1.py", 1, ".claude/settings.json", "root:settings",
         ".claude/settings.json"),
        ("scripts", "global/scripts/script-s1.py", 2, ".claude/settings.json",
         "root:settings", "hook-h1.py"),
        ("scripts", "global/scripts/script-s2.py", 3, ".claude/settings.json",
         "root:settings", "script-s1.py"),
        ("scripts", "global/scripts/script-s7.py", 2, ".claude/settings.json",
         "root:settings", "hook-h2.py"),
        ("scripts", "global/scripts/script-s8.py", 3, ".claude/settings.json",
         "root:settings", "script-s7.py"),
        ("scripts", "global/scripts/script-dbottom.py", 3, ".claude/settings.json",
         "root:settings", "script-dleft.py"),
        ("scripts", "global/scripts/script-dtail.py", 4, ".claude/settings.json",
         "root:settings", "script-dbottom.py"),
        ("scripts", "global/scripts/run-tests.py", 1, ".claude/settings.json",
         "root:settings", ".claude/settings.json"),
        ("scripts", "global/scripts/test-wired.py", 2, ".claude/settings.json",
         "root:settings", "run-tests.py"),
        ("hooks", "global/hooks/hook-manifest.py", 1, "hooks-manifest.json",
         "root:manifest", "hooks-manifest.json"),
        ("scripts", "global/scripts/script-launcher.py", 1, "mylauncher",
         "root:launcher", "mylauncher"),
        ("scripts", "global/scripts/script-unit.py", 1, "x.service", "root:unit",
         "x.service"),
    )
    for cls, key, length, first_suffix, first_via, last_suffix in chain_cases:
        name = os.path.basename(key)
        ok, detail = reach_is(report, cls, key, "root-reachable")
        check("%s is root-reachable" % name, ok, detail)
        ok, detail = chain_ok(report, cls, key, length, first_suffix, first_via, last_suffix)
        check("%s chain: %d steps from %s" % (name, length, first_via), ok, detail)
    ok, detail = chain_ok(report, "scripts", "global/scripts/script-s2.py", 3,
                          ".claude/settings.json", "root:settings")
    chain = chain_of(report, "scripts", "global/scripts/script-s2.py") or []
    check("S2 chain files run settings.json, hook-h1.py, script-s1.py in order",
          [os.path.basename(s.get("file", "")) for s in chain]
          == ["settings.json", "hook-h1.py", "script-s1.py"],
          str([s.get("file") for s in chain]))

    print("R7.3: cycles and diamond")
    for key in ("script-s5.py", "script-s6.py"):
        ok, detail = reach_is(report, "scripts", "global/scripts/" + key, "unreachable")
        check("%s (cycle with no root entry) is unreachable" % key, ok, detail)
    s5_chain = chain_of(report, "scripts", "global/scripts/script-s5.py") or []
    check("S5 chain lists reader script-s6.py with its own reach unreachable",
          any(str(s.get("file", "")).endswith("script-s6.py")
              and s.get("reach") == "unreachable" for s in s5_chain), str(s5_chain)[:200])
    for key in ("script-s7.py", "script-s8.py"):
        ok, detail = reach_is(report, "scripts", "global/scripts/" + key, "root-reachable")
        check("%s (cycle entered from root hook) is root-reachable" % key, ok, detail)
    ok, detail = reach_is(report, "scripts", "global/scripts/script-dbottom.py",
                          "root-reachable")
    check("diamond bottom is root-reachable", ok, detail)

    print("R7.2/R7.4: test-only")
    for key in ("script-s3.py", "script-s3-child.py", "script-behind-hook-test.py",
                "script-behind-test-under.py", "script-behind-test.py"):
        ok, detail = reach_is(report, "scripts", "global/scripts/" + key, "test-only")
        check("%s is test-only" % key, ok, detail)
    s3_chain = chain_of(report, "scripts", "global/scripts/script-s3.py") or []
    check("S3 chain lists reader test-s3.py with a reach field",
          any(str(s.get("file", "")).endswith("test-s3.py") and "reach" in s
              for s in s3_chain), str(s3_chain)[:200])
    ok, detail = reach_is(report, "scripts", "global/scripts/test-s3.py", "unreachable")
    check("test-s3.py (unwired test file) is itself unreachable", ok, detail)
    ok, detail = reach_is(report, "scripts", "global/scripts/test-wired.py", "root-reachable")
    check("test-wired.py (run by wired run-tests.py) is root-reachable", ok, detail)
    ok, detail = reach_is(report, "hooks", "global/hooks/hook-behind-test.py", "uncertain")
    check("uncertain beats test-only (hook behind test-s3.py, matched by a glob)", ok, detail)

    print("R7.2: unreachable")
    for key in ("script-s4.py", "script-orphan.py"):
        ok, detail = reach_is(report, "scripts", "global/scripts/" + key, "unreachable")
        check("%s is unreachable" % key, ok, detail)

    print("R7.5: dynamic references")
    glob_line = line_of(GLOB_LOADER, "glob.glob")
    fstring_line = line_of(FSTRING_LOADER, "{name}.sh")
    for cls, key, want in (("hooks", "global/hooks/hook-h9.py", "uncertain"),
                           ("scripts", "global/scripts/dyn-target.sh", "uncertain"),
                           ("hooks", "global/hooks/hook-h1.py", "root-reachable")):
        ok, detail = reach_is(report, cls, key, want)
        check("%s is %s" % (os.path.basename(key), want), ok, detail)
    h9 = entry(report, "hooks", "global/hooks/hook-h9.py") or {}
    via = [u for u in h9.get("uncertain_via", []) if isinstance(u, dict)]
    check("hook-h9.py uncertain_via names unreachable-loader.py at its glob line",
          any(str(u.get("file", "")).endswith("unreachable-loader.py")
              and u.get("line") == glob_line for u in via), str(via)[:200])
    target = entry(report, "scripts", "global/scripts/dyn-target.sh") or {}
    via = [u for u in target.get("uncertain_via", []) if isinstance(u, dict)]
    check("dyn-target.sh uncertain_via names hook-loader.py at its f-string line",
          any(str(u.get("file", "")).endswith("hook-loader.py")
              and u.get("line") == fstring_line for u in via), str(via)[:200])
    loader_lines = (report or {}).get("loader_lines")
    check("report has a loader_lines list",
          isinstance(loader_lines, list) and bool(loader_lines), str(loader_lines)[:100])
    loaders = [ln for ln in loader_lines if isinstance(ln, dict)] \
        if isinstance(loader_lines, list) else []
    glob_entries = [ln for ln in loaders
                    if str(ln.get("file", "")).endswith("unreachable-loader.py")
                    and ln.get("line") == glob_line]
    check("loader_lines has the glob loader once, with snippet",
          len(glob_entries) == 1 and "glob.glob" in str(glob_entries[0].get("snippet")),
          str(loaders)[:300])
    check("glob loader matched_keys is an int >= 2 (hook-h9.py and more)",
          bool(glob_entries) and isinstance(glob_entries[0].get("matched_keys"), int)
          and glob_entries[0]["matched_keys"] >= 2, str(glob_entries)[:200])
    fstring_entries = [ln for ln in loaders
                       if str(ln.get("file", "")).endswith("hook-loader.py")
                       and ln.get("line") == fstring_line]
    check("loader_lines has the f-string loader once with matched_keys == 1",
          len(fstring_entries) == 1 and fstring_entries[0].get("matched_keys") == 1,
          str(loaders)[:300])

    print("R7.6: docs")
    ok, detail = reach_is(report, "docs", "shared-docs/doc-d1.md", "root-reachable")
    check("doc read by path from a command shell body is root-reachable", ok, detail)
    ok, detail = chain_ok(report, "docs", "shared-docs/doc-d1.md", 1, "cmd-d1.md",
                          "root:command-shell", "cmd-d1.md")
    check("doc-d1.md chain is the command's shell line via root:command-shell", ok, detail)
    ok, detail = reach_is(report, "docs", "shared-docs/doc-d2.md", "root-reachable")
    check("doc read by path from a root-reachable script is root-reachable", ok, detail)
    ok, detail = chain_ok(report, "docs", "shared-docs/doc-d2.md", 2, ".claude/settings.json",
                          "root:settings", "hook-doc-reader.py")
    check("doc-d2.md chain: settings.json then hook-doc-reader.py", ok, detail)
    ok, detail = reach_is(report, "docs", "shared-docs/doc-d3.md", "unreachable")
    check("doc read by path from an unreachable script is unreachable", ok, detail)
    d3_chain = chain_of(report, "docs", "shared-docs/doc-d3.md") or []
    check("doc-d3.md chain lists script-doc3-reader.py with reach unreachable",
          any(str(s.get("file", "")).endswith("script-doc3-reader.py")
              and s.get("reach") == "unreachable" for s in d3_chain), str(d3_chain)[:200])
    ok, detail = reach_is(report, "docs", "shared-docs/doc-d4.md", "unreachable")
    check("doc named only in prose is unreachable", ok, detail)

    print("R7.7: unchecked roots and caveats")
    roots = report.get("unchecked_roots") if isinstance(report, dict) else None
    check("unchecked_roots default is m4, pc, rp, mirror-a, mirror-b and the laptop root",
          isinstance(roots, list) and sorted(roots) == sorted(DEFAULT_ROOTS), str(roots))
    rc, partial, _err = run_json(root, ["--checked", "m4", "--checked", "rp"])
    partial_roots = partial.get("unchecked_roots") if isinstance(partial, dict) else None
    check("--checked m4 --checked rp removes exactly those roots",
          isinstance(partial_roots, list)
          and sorted(partial_roots) == sorted(["pc", "mirror-a", "mirror-b", LAPTOP_ROOT]),
          str(partial_roots))
    rc, done, _err = run_json(root, ALL_CHECKED)
    done_roots = done.get("unchecked_roots") if isinstance(done, dict) else None
    check("all machines checked leaves only the laptop root",
          done_roots == [LAPTOP_ROOT], str(done_roots))
    rc, laptop, _err = run_json(root, ["--checked", LAPTOP_ROOT])
    laptop_roots = laptop.get("unchecked_roots") if isinstance(laptop, dict) else None
    check("--checked cannot remove the laptop root",
          isinstance(laptop_roots, list) and LAPTOP_ROOT in laptop_roots, str(laptop_roots))
    check("unchecked_machines still shrinks with --checked (old behavior)",
          isinstance(partial, dict)
          and partial.get("unchecked_machines") == ["pc", "mirror-a", "mirror-b"]
          and isinstance(done, dict) and done.get("unchecked_machines") == [])
    caveat_keys = (("scripts", "global/scripts/script-s3.py"),
                   ("scripts", "global/scripts/script-s3-child.py"),
                   ("scripts", "global/scripts/script-s4.py"),
                   ("scripts", "global/scripts/script-s5.py"),
                   ("scripts", "global/scripts/script-orphan.py"),
                   ("docs", "shared-docs/doc-d3.md"), ("docs", "shared-docs/doc-d4.md"))
    for cls, key in caveat_keys:
        info = entry(report, cls, key)
        check("%s (%s) has a caveat naming every unchecked root"
              % (os.path.basename(key), info.get("reach") if info else None),
              bool(info) and info.get("reach") in ("test-only", "unreachable")
              and has_caveat_naming(info, roots if isinstance(roots, list) else []),
              str(info.get("caveat") if info else None)[:200])
    for cls, key in caveat_keys[:3]:
        info = entry(done, cls, key)
        text = info.get("caveat") if info else None
        check("%s caveat with all machines checked names the laptop root only"
              % os.path.basename(key),
              isinstance(text, str) and LAPTOP_ROOT in text
              and not any(m in text for m in ("m4", "pc", "rp", "mirror-a", "mirror-b")),
              str(text)[:200])

    print("R7.8: report-only and text summary")
    out_path = os.path.join(root, "outdir", "report.json")
    rc_out, _stdout, _err = run_tool(
        ["--store", os.path.join(root, "store"), "--home", os.path.join(root, "home"),
         "--json", "--out", out_path], {"HOME": root})
    written = None
    if os.path.isfile(out_path):
        with open(out_path, "r", encoding="utf-8") as handle:
            try:
                written = json.load(handle)
            except ValueError:
                written = None
    check("--out report carries reach, chain, loader_lines and unchecked_roots",
          rc_out == 0 and isinstance(written, dict)
          and all(k in written for k in ("loader_lines", "unchecked_roots"))
          and (entry(written, "scripts", "global/scripts/script-s2.py") or {}).get("reach")
          == "root-reachable" and isinstance(
              (entry(written, "scripts", "global/scripts/script-s2.py") or {}).get("chain"),
              list), "rc=%s" % rc_out)
    rc_text, text, _err = run_tool(
        ["--store", os.path.join(root, "store"), "--home", os.path.join(root, "home")],
        {"HOME": root})
    counts_re = re.compile(
        r"root-reachable=(\d+) test-only=(\d+) unreachable=(\d+) uncertain=(\d+)")
    for cls in ("hooks", "scripts", "docs"):
        summary = next((ln for ln in text.splitlines() if ln.startswith(cls + ":")), "")
        match = counts_re.search(summary)
        keys = ((report or {}).get("classes", {}).get(cls, {}).get("keys", {}))
        want = tuple(sum(1 for e in keys.values() if e.get("reach") == value)
                     for value in REACH_VALUES)
        check("text summary for %s has root-reachable/test-only/unreachable/uncertain counts"
              % cls,
              rc_text == 0 and bool(match) and bool(keys)
              and tuple(int(g) for g in match.groups()) == want,
              "line %r, want %s" % (summary[:200], want))
    after = snapshot(os.path.join(root, "store"), os.path.join(root, "home"))
    check("fixture store and home are byte-identical after every run incl. --out",
          rc_out == 0 and before == after,
          "changed: %s" % sorted(k for k in set(before) | set(after)
                                 if before.get(k) != after.get(k))[:5])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

total = passed + len(failures)
print("test-store-reader-graph-reach: %d/%d passed" % (passed, total))
sys.exit(1 if failures else 0)
