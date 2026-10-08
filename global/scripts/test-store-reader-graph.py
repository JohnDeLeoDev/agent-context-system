#!/usr/bin/env python3
'Battery for store-reader-graph.py, the read-only reader-graph evidence tool.\n\nBuilds fixture stores and homes under ~/.cache/tmp with a known reader graph, runs the\nreal script against them and checks verdicts, evidence, report fields, the read-only\nguarantee and malformed-input tolerance. stdlib only.'

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "store-reader-graph.py")
CLASSES = ("hooks", "scripts", "docs", "skills", "commands", "agents", "manifests")
DEFAULT_UNCHECKED = ["m4", "rp", "pc", "mirror-a", "mirror-b"]

scratch_root = os.path.join(os.path.expanduser("~"), ".cache", "tmp")
os.makedirs(scratch_root, exist_ok=True)
tmp = tempfile.mkdtemp(prefix="store-reader-graph-test-", dir=scratch_root)

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


def write(root, rel, content, mode=None):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = content if isinstance(content, bytes) else content.encode()
    with open(path, "wb") as handle:
        handle.write(data)
    if mode is not None:
        os.chmod(path, mode)
    return path


SETTINGS_JSON = """{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 $HOME/.agent-context/global/hooks/hook-settings.py"
          }
        ]
      }
    ]
  }
}
"""

MAIN_FILES = {
    
    "store/global/hooks/hook-settings.py": "print('settings')\n",
    "store/global/hooks/hook-codex.py": "print('codex')\n",
    "store/global/hooks/hook-subproc.py": "print('subproc')\n",
    "store/global/hooks/hook-orphan.py": "print('nobody')\n",
    "store/global/hooks/hook-manifest.py": "print('manifest')\n",
    "store/global/hooks/hook-opencode.py": "print('opencode')\n",
    "store/global/hooks/hook-copilot.py": "print('copilot')\n",
    "store/global/hooks/hook-pi.py": "print('pi')\n",
    "store/global/hooks/hook-self.py": "# hook-self.py: this file names itself\nprint('self')\n",
    
    "store/global/scripts/runner.py": (
        "import importlib.util, os, subprocess\n"
        "HERE = os.path.dirname(os.path.abspath(__file__))\n"
        "subprocess.run([\"python3\", os.path.expanduser("
        "\"~/.agent-context/global/hooks/hook-subproc.py\")])\n"
        "subprocess.run([\"python3\", \"script-argv.py\"], cwd=HERE)\n"
        "target = os.path.join(HERE, \"script-joined.py\")\n"
        "spec = importlib.util.spec_from_file_location(\"m\", "
        "os.path.join(HERE, \"script-imported.py\"))\n"
    ),
    "store/global/scripts/sync.py": (
        "import os\n"
        "STORE = os.path.expanduser(\"~/.agent-context\")\n"
        "path = os.path.join(STORE, \"global\", \"hooks-manifest.json\")\n"
    ),
    "store/global/scripts/script-argv.py": "print('argv')\n",
    "store/global/scripts/script-joined.py": "print('joined')\n",
    "store/global/scripts/script-imported.py": "print('imported')\n",
    "store/global/scripts/script-doc-mentioned.py": "print('doc')\n",
    "store/global/scripts/script-orphan.py": "print('nobody')\n",
    "store/global/scripts/script-launcher.py": "print('launcher')\n",
    "store/global/scripts/script-unit.py": "print('unit')\n",
    "store/global/scripts/script-plist.py": "print('plist')\n",
    "store/global/scripts/script-mcp.py": "print('mcp')\n",
    "store/global/scripts/script-self.py": "# usage: script-self.py\nprint('self')\n",
    "store/global/scripts/binary.py": b"\x00\xff\xfe\x80" * 64,
    
    "store/shared-docs/doc-by-command.md": "# By command\n",
    "store/shared-docs/doc-prose.md": "# Prose\n",
    "store/shared-docs/doc-orphan.md": "# Orphan\n",
    "store/shared-docs/guide.md": (
        "# Guide\n\nSee doc-prose.md for background. The script "
        "script-doc-mentioned.py is described here, and so is mcp-servers.json.\n"
    ),
    
    "store/global/commands/cmd.md": (
        "Run the check.\n\n```bash\n"
        "cat \"$HOME/.agent-context/shared-docs/doc-by-command.md\"\n```\n"
    ),
    "store/global/skills/skill-named/SKILL.md": "# skill-named\n",
    "store/global/skills/skill-orphan/SKILL.md": "# skill-orphan\n",
    "store/global/agents/agent1.md": "Invoke the skill `skill-named` first.\n",
    
    "store/global/hooks-manifest.json": (
        "{\n  \"hooks\": {\n    \"hook-manifest.py\": {\"sha256\": \"abc\"}\n  }\n}\n"
    ),
    "store/global/mcp-servers.json": (
        "{\n  \"servers\": {\n    \"demo\": {\n      \"command\": \"python3\",\n"
        "      \"args\": [\"/home/x/.agent-context/global/scripts/script-mcp.py\"]\n"
        "    }\n  }\n}\n"
    ),
    
    "home/.claude/settings.json": SETTINGS_JSON,
    "home/.codex/hooks.json": (
        "{\n  \"hooks\": {\n    \"PreToolUse\": [{\"command\": "
        "\"python3 ~/.agent-context/global/hooks/hook-codex.py\"}]\n  }\n}\n"
    ),
    "home/.config/opencode/opencode.json": (
        "{\n  \"hook\": \"python3 ~/.agent-context/global/hooks/hook-opencode.py\"\n}\n"
    ),
    "home/.config/opencode/bad.json": "{ this is not json",
    "home/.copilot/config.json": (
        "{\n  \"hook\": \"python3 ~/.agent-context/global/hooks/hook-copilot.py\"\n}\n"
    ),
    "home/.pi/agent/extensions/ext.ts": (
        "const cmd = \"python3 ~/.agent-context/global/hooks/hook-pi.py\";\n"
    ),
    "home/.local/bin/mylauncher": (
        "#!/bin/sh\nexec python3 \"$HOME/.agent-context/global/scripts/"
        "script-launcher.py\" \"$@\"\n"
    ),
    "home/Library/LaunchAgents/x.plist": (
        "<plist><dict><key>ProgramArguments</key><array>"
        "<string>/Users/x/.agent-context/global/scripts/script-plist.py</string>"
        "</array></dict></plist>\n"
    ),
    "home/.config/systemd/user/x.service": (
        "[Service]\nExecStart=/usr/bin/python3 %h/.agent-context/global/scripts/"
        "script-unit.py\n"
    ),
}

DYNAMIC_CASES = {
    "computed-name": (
        {
            "store/global/scripts/dyn-loader.py": (
                "import os\npath = os.path.join(scripts_dir, name + \".py\")\n"
            ),
            "store/global/scripts/dyn-a.py": "print('a')\n",
            "store/global/scripts/dyn-b.py": "print('b')\n",
        },
        ("scripts", ["global/scripts/dyn-a.py", "global/scripts/dyn-b.py"], "dyn-loader.py"),
    ),
    "glob": (
        {
            "store/global/scripts/glob-loader.py": (
                "import glob, os\n"
                "for p in glob.glob(os.path.join(ROOT, \"hooks/*.py\")):\n    pass\n"
            ),
            "store/global/hooks/g1.py": "print('g1')\n",
            "store/global/hooks/g2.py": "print('g2')\n",
        },
        ("hooks", ["global/hooks/g1.py", "global/hooks/g2.py"], "glob-loader.py"),
    ),
    "f-string": (
        {
            "store/global/scripts/f-loader.py": (
                "import subprocess\n"
                "subprocess.run([\"python3\", f\"{HOOKS}/{name}.py\"])\n"
            ),
            "store/global/hooks/f1.py": "print('f1')\n",
        },
        ("hooks", ["global/hooks/f1.py"], "f-loader.py"),
    ),
    "directory-listing": (
        {
            "store/global/scripts/l-loader.py": (
                "import os\n"
                "for n in os.listdir(os.path.join(STORE, \"global\", \"hooks\")):\n"
                "    pass\n"
            ),
            "store/global/hooks/l1.py": "print('l1')\n",
        },
        ("hooks", ["global/hooks/l1.py"], "l-loader.py"),
    ),
}


def build(name, files, mode_overrides=None):
    root = os.path.join(tmp, name)
    for rel, content in files.items():
        write(root, rel, content, (mode_overrides or {}).get(rel))
    os.makedirs(os.path.join(root, "outdir"), exist_ok=True)
    os.makedirs(os.path.join(root, "home"), exist_ok=True)
    return root


def snapshot(*roots):
    'Relative path -> sha256 (or mode when unreadable) for every file under roots.'
    state = {}
    for root in roots:
        for base, _dirs, names in os.walk(root):
            for name in names:
                path = os.path.join(base, name)
                try:
                    with open(path, "rb") as handle:
                        state[path] = hashlib.sha256(handle.read()).hexdigest()
                except OSError:
                    state[path] = "unreadable:%o" % os.stat(path).st_mode
    return state


def run_tool(args, env_overrides=None):
    env = dict(os.environ)
    env.pop("AGENT_CONTEXT_STORE", None)
    env.update(env_overrides or {})
    proc = subprocess.run([sys.executable, TOOL] + args, capture_output=True,
                          text=True, env=env, timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


def run_json(root, extra=None, env_overrides=None):
    'Run with explicit --store/--home/--json; return (rc, report or None, stderr).'
    env = {"HOME": root}
    env.update(env_overrides or {})
    args = ["--store", os.path.join(root, "store"), "--home", os.path.join(root, "home"),
            "--json"] + (extra or [])
    rc, out, err = run_tool(args, env)
    try:
        return rc, json.loads(out), err
    except ValueError:
        return rc, None, err + out[:300]


def entry(report, cls, key):
    if not isinstance(report, dict):
        return None
    classes = report.get("classes")
    if not isinstance(classes, dict):
        return None
    section = classes.get(cls)
    if not isinstance(section, dict):
        return None
    keys = section.get("keys")
    if not isinstance(keys, dict):
        return None
    found = keys.get(key)
    return found if isinstance(found, dict) else None


def verdict_is(report, cls, key, expected):
    info = entry(report, cls, key)
    got = info.get("verdict") if info else None
    return got == expected, "verdict %r, want %r" % (got, expected)


def readers_of(report, cls, key):
    info = entry(report, cls, key)
    readers = info.get("readers") if info else None
    return [r for r in readers if isinstance(r, dict)] if isinstance(readers, list) else []


try:
    print("fixture sanity")
    main = build("main", MAIN_FILES, {"store/global/scripts/locked.py": 0})
    locked = write(main, "store/global/scripts/locked.py", "print('locked')\n", 0)
    is_root = os.geteuid() == 0
    check("fixture: hooks, scripts, docs, home configs exist",
          all(os.path.isfile(os.path.join(main, rel)) for rel in (
              "store/global/hooks/hook-settings.py", "store/global/scripts/runner.py",
              "store/shared-docs/doc-orphan.md", "home/.claude/settings.json",
              "home/.local/bin/mylauncher", "store/global/mcp-servers.json")))
    check("fixture: locked file is unreadable to this user",
          is_root or not os.access(locked, os.R_OK))
    check("fixture: no global/docs directory in the main store",
          not os.path.exists(os.path.join(main, "store/global/docs")))
    tool_present = os.path.isfile(TOOL)
    check("tool exists: global/scripts/store-reader-graph.py", tool_present, TOOL)

    before = snapshot(os.path.join(main, "store"), os.path.join(main, "home"))

    print("R1: interface")
    rc, report, err = run_json(main)
    check("--json run exits 0 and prints JSON", rc == 0 and report is not None,
          "rc=%s %s" % (rc, err[:200]))
    check("report has host, store, classes, unchecked_machines",
          isinstance(report, dict) and all(
              k in report for k in ("host", "store", "classes", "unchecked_machines")))
    check("all seven classes reported by default",
          isinstance(report, dict) and isinstance(report.get("classes"), dict)
          and set(report["classes"]) == set(CLASSES),
          str(sorted(report["classes"])) if isinstance(report, dict)
          and isinstance(report.get("classes"), dict) else "no classes")

    rc, hooks_only, err = run_json(main, ["--class", "hooks"])
    check("--class hooks limits classes to hooks",
          rc == 0 and isinstance(hooks_only, dict)
          and list(hooks_only.get("classes", {})) == ["hooks"], "rc=%s" % rc)
    rc, _out, err = run_tool(["--store", os.path.join(main, "store"), "--class", "bogus"],
                             {"HOME": main})
    check("--class with an unknown class exits 2 with usage",
          rc == 2 and "usage" in err.lower(), "rc=%s err=%s" % (rc, err[:120]))

    rc, text, err = run_tool(["--store", os.path.join(main, "store"),
                              "--home", os.path.join(main, "home")], {"HOME": main})
    lines = text.splitlines()
    check("text mode: one line per key with its verdict",
          rc == 0 and any("global/hooks/hook-orphan.py" in ln and "no-reader-found" in ln
                          for ln in lines)
          and any("global/hooks/hook-settings.py" in ln and "needed-on-disk" in ln
                  for ln in lines), "rc=%s %s" % (rc, text[:200]))
    check("text mode: one summary line per class",
          rc == 0 and all(any(cls in ln and "global/" not in ln for ln in lines)
                          for cls in CLASSES), "rc=%s" % rc)
    check("text mode is not JSON", rc == 0 and not text.lstrip().startswith("{"))

    rc, defaults, err = run_tool(["--json"], {
        "AGENT_CONTEXT_STORE": os.path.join(main, "store"),
        "HOME": os.path.join(main, "home")})
    try:
        default_report = json.loads(defaults)
    except ValueError:
        default_report = None
    check("--store defaults to $AGENT_CONTEXT_STORE and --home to $HOME",
          rc == 0 and verdict_is(default_report, "hooks", "global/hooks/hook-settings.py",
                                 "needed-on-disk")[0], "rc=%s" % rc)

    print("R5: report header")
    check("host is the hostname",
          isinstance(report, dict) and report.get("host") == socket.gethostname(),
          str(report.get("host")) if isinstance(report, dict) else "no report")
    check("store is the fixture store path",
          isinstance(report, dict) and isinstance(report.get("store"), str)
          and os.path.realpath(report["store"]) == os.path.realpath(
              os.path.join(main, "store")),
          str(report.get("store")) if isinstance(report, dict) else "no report")
    counts_ok = isinstance(report, dict) and isinstance(report.get("classes"), dict)
    expected_min = {"hooks": 9, "scripts": 12, "docs": 4, "skills": 2, "commands": 1,
                    "agents": 1, "manifests": 2}
    if counts_ok and report is not None:
        for cls, minimum in expected_min.items():
            section = report["classes"].get(cls)
            got = section.get("files_searched") if isinstance(section, dict) else None
            check("files_searched for %s is an int >= %d" % (cls, minimum),
                  isinstance(got, int) and got >= minimum, "got %r" % (got,))
    else:
        check("files_searched per class", False, "no classes in report")
    check("unchecked_machines defaults to the five other machines",
          isinstance(report, dict) and report.get("unchecked_machines") == DEFAULT_UNCHECKED,
          str(report.get("unchecked_machines")) if isinstance(report, dict) else "none")
    rc, partial, _err = run_json(main, ["--checked", "m4", "--checked", "rp"])
    check("--checked is repeatable and removes names",
          rc == 0 and isinstance(partial, dict)
          and partial.get("unchecked_machines") == ["pc", "mirror-a", "mirror-b"],
          str(partial.get("unchecked_machines")) if isinstance(partial, dict) else "none")
    rc, done, _err = run_json(main, ["--checked", "m4", "--checked", "rp", "--checked", "pc",
                                     "--checked", "mirror-a", "--checked", "mirror-b"])
    orphan = entry(done, "hooks", "global/hooks/hook-orphan.py")
    orphan_default = entry(report, "hooks", "global/hooks/hook-orphan.py")
    check("no-reader-found key carries a caveat while machines are unchecked",
          bool(orphan_default and isinstance(orphan_default.get("caveat"), str)
               and orphan_default["caveat"].strip()))
    check("caveat is empty once every machine is checked",
          bool(orphan) and done is not None and not orphan.get("caveat")
          and done.get("unchecked_machines") == [],
          "rc=%s" % rc)

    print("R2/R3: hooks")
    settings_line = next(i for i, ln in enumerate(SETTINGS_JSON.splitlines(), 1)
                         if "hook-settings.py" in ln)
    hook_cases = [
        ("hook-settings.py", "needed-on-disk", ".claude/settings.json", settings_line),
        ("hook-codex.py", "needed-on-disk", ".codex/hooks.json", None),
        ("hook-opencode.py", "needed-on-disk", ".config/opencode/opencode.json", None),
        ("hook-copilot.py", "needed-on-disk", ".copilot/config.json", None),
        ("hook-pi.py", "needed-on-disk", ".pi/agent/extensions/ext.ts", None),
        ("hook-subproc.py", "needed-on-disk", "scripts/runner.py", None),
        ("hook-manifest.py", "needed-on-disk", "hooks-manifest.json", None),
    ]
    for name, want, reader_suffix, line in hook_cases:
        key = "global/hooks/" + name
        ok, detail = verdict_is(report, "hooks", key, want)
        check("%s is %s" % (name, want), ok, detail)
        matches = [r for r in readers_of(report, "hooks", key)
                   if r.get("kind") == "exec" and str(r.get("file", "")).endswith(reader_suffix)
                   and isinstance(r.get("line"), int) and r["line"] >= 1
                   and name in str(r.get("snippet", ""))]
        if line is not None:
            matches = [r for r in matches if r["line"] == line]
        check("%s has an exec reader in %s with file:line and snippet" % (name, reader_suffix),
              bool(matches), str(readers_of(report, "hooks", key))[:300])
    ok, detail = verdict_is(report, "hooks", "global/hooks/hook-orphan.py", "no-reader-found")
    check("hook nobody names is no-reader-found", ok, detail)
    ok, detail = verdict_is(report, "hooks", "global/hooks/hook-self.py", "no-reader-found")
    check("a self-reference does not count as a reader", ok, detail)

    print("R2/R3: scripts")
    script_cases = [
        ("script-launcher.py", "needed-on-disk", "home/.local/bin/mylauncher".split("/")[-1]),
        ("script-plist.py", "needed-on-disk", "x.plist"),
        ("script-unit.py", "needed-on-disk", "x.service"),
        ("script-mcp.py", "needed-on-disk", "mcp-servers.json"),
        ("script-argv.py", "needed-on-disk", "runner.py"),
        ("script-joined.py", "needed-on-disk", "runner.py"),
        ("script-imported.py", "needed-on-disk", "runner.py"),
        ("script-doc-mentioned.py", "mention-only", "guide.md"),
        ("script-orphan.py", "no-reader-found", None),
        ("script-self.py", "no-reader-found", None),
    ]
    for name, want, reader_suffix in script_cases:
        key = "global/scripts/" + name
        ok, detail = verdict_is(report, "scripts", key, want)
        check("%s is %s" % (name, want), ok, detail)
        if reader_suffix:
            kind = "mention" if want == "mention-only" else "exec"
            matches = [r for r in readers_of(report, "scripts", key)
                       if r.get("kind") == kind
                       and str(r.get("file", "")).endswith(reader_suffix)
                       and isinstance(r.get("line"), int)]
            check("%s has a %s reader in %s" % (name, kind, reader_suffix), bool(matches),
                  str(readers_of(report, "scripts", key))[:300])
    mention_only_readers = readers_of(report, "scripts", "global/scripts/script-doc-mentioned.py")
    check("a doc mention is never kind exec",
          bool(mention_only_readers) and all(r.get("kind") == "mention"
                                             for r in mention_only_readers))

    print("R2/R3: docs, skills, commands, agents, manifests")
    for key, want in (("shared-docs/doc-by-command.md", "needed-on-disk"),
                      ("shared-docs/doc-prose.md", "mention-only"),
                      ("shared-docs/doc-orphan.md", "no-reader-found")):
        ok, detail = verdict_is(report, "docs", key, want)
        check("%s is %s" % (key, want), ok, detail)
    by_command = [r for r in readers_of(report, "docs", "shared-docs/doc-by-command.md")
                  if r.get("kind") == "exec" and str(r.get("file", "")).endswith("cmd.md")
                  and isinstance(r.get("line"), int)]
    check("doc read by path in a command's bash block has an exec reader in cmd.md",
          bool(by_command))
    ok, detail = verdict_is(report, "skills", "global/skills/skill-named/SKILL.md",
                            "mention-only")
    check("skill named by an agent is mention-only", ok, detail)
    ok, detail = verdict_is(report, "skills", "global/skills/skill-orphan/SKILL.md",
                            "no-reader-found")
    check("skill nobody names is no-reader-found", ok, detail)
    check("commands class lists global/commands/cmd.md",
          entry(report, "commands", "global/commands/cmd.md") is not None)
    check("agents class lists global/agents/agent1.md",
          entry(report, "agents", "global/agents/agent1.md") is not None)
    ok, detail = verdict_is(report, "manifests", "global/hooks-manifest.json", "needed-on-disk")
    check("hooks-manifest.json read by a script is needed-on-disk", ok, detail)
    ok, detail = verdict_is(report, "manifests", "global/mcp-servers.json", "mention-only")
    check("mcp-servers.json named only in a doc is mention-only", ok, detail)

    print("R3: dynamic references")
    for label, (files, (cls, keys, loader)) in DYNAMIC_CASES.items():
        root = build("dyn-" + label, files)
        write(root, "home/.claude/settings.json", "{}\n")
        for rel in files:
            check("fixture: %s file exists" % rel, os.path.isfile(os.path.join(root, rel)))
        rc, dyn, err = run_json(root)
        for key in keys:
            info = entry(dyn, cls, key)
            via = json.dumps(info.get("uncertain_via")) if info else ""
            check("%s reference makes %s uncertain" % (label, key),
                  bool(info) and info.get("verdict") == "uncertain",
                  "rc=%s verdict=%s" % (rc, info.get("verdict") if info else None))
            check("%s: %s lists %s in uncertain_via" % (label, key, loader),
                  bool(info) and isinstance(info.get("uncertain_via"), list)
                  and loader in via, via[:200])

    print("R4: malformed input")
    unreadable = report.get("unreadable") if isinstance(report, dict) else None
    dumped = json.dumps(unreadable)
    check("report has an unreadable list", isinstance(unreadable, list) and bool(unreadable))
    check("invalid JSON config is noted as unreadable", "bad.json" in dumped, dumped[:200])
    check("binary file in scripts is noted as unreadable", "binary.py" in dumped, dumped[:200])
    if not is_root:
        check("permission-denied file is noted as unreadable", "locked.py" in dumped,
              dumped[:200])
    ok, detail = verdict_is(report, "hooks", "global/hooks/hook-settings.py", "needed-on-disk")
    check("run continues past malformed files", ok and report is not None, detail)

    print("R4: --out and read-only guarantee")
    out_path = os.path.join(main, "outdir", "report.json")
    rc_out, out_stdout, _err = run_tool(
        ["--store", os.path.join(main, "store"), "--home", os.path.join(main, "home"),
         "--json", "--out", out_path], {"HOME": main})
    written = None
    if os.path.isfile(out_path):
        with open(out_path, "r", encoding="utf-8") as handle:
            try:
                written = json.load(handle)
            except ValueError:
                written = None
    check("--out writes the JSON report under $HOME",
          rc_out == 0 and isinstance(written, dict)
          and set(written.get("classes", {})) == set(CLASSES)
          and written.get("host") == socket.gethostname(), "rc=%s" % rc_out)
    refuse_path = "/usr/reader-graph-refuse-test.json"
    rc_refuse, _o, _e = run_tool(
        ["--store", os.path.join(main, "store"), "--home", os.path.join(main, "home"),
         "--json", "--out", refuse_path], {"HOME": main})
    check("--out outside $HOME is refused with a nonzero exit and no file",
          rc_out == 0 and rc_refuse != 0 and not os.path.exists(refuse_path),
          "rc=%s" % rc_refuse)
    after = snapshot(os.path.join(main, "store"), os.path.join(main, "home"))
    check("fixture store and home are byte-identical after every run",
          rc_out == 0 and before == after,
          "changed: %s" % sorted(k for k in set(before) | set(after)
                                 if before.get(k) != after.get(k))[:5])

    print("R6: no mutating flags, no mutating code")
    for flag in ("--apply", "--delete", "--fix"):
        rc, out, err = run_tool(["--store", os.path.join(main, "store"), flag],
                                {"HOME": main})
        check("%s exits 2 with usage" % flag, rc == 2 and "usage" in (err + out).lower(),
              "rc=%s %s" % (rc, (err + out)[:120]))
    pattern = re.compile(
        r"os\.remove|unlink|rmtree|shutil\.move|os\.rename|write_text|open\(.*[\"']w")
    if tool_present:
        with open(TOOL, "r", encoding="utf-8") as handle:
            source_lines = handle.read().splitlines()
        offenders = [(i, ln.strip()) for i, ln in enumerate(source_lines, 1)
                     if pattern.search(ln) and not re.search(r"\bout", ln, re.I)]
        check("source removes or edits nothing outside the --out writer",
              not offenders, str(offenders[:5]))
        check("source has an --out writer", any(
            re.search(r"open\(.*[\"']w|write_text", ln) and re.search(r"\bout", ln, re.I)
            for ln in source_lines))
    else:
        check("source removes or edits nothing outside the --out writer", False,
              "tool script missing")
finally:
    for base, dirs, _names in os.walk(tmp):
        for name in dirs:
            os.chmod(os.path.join(base, name), 0o700)
    for base, _dirs, names in os.walk(tmp):
        for name in names:
            try:
                os.chmod(os.path.join(base, name), 0o600)
            except OSError:
                pass
    shutil.rmtree(tmp, ignore_errors=True)

total = passed + len(failures)
print("test-store-reader-graph: %d/%d passed" % (passed, total))
sys.exit(1 if failures else 0)
