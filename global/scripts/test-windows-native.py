#!/usr/bin/env python3
'Runs on any host. It loads the scripts beside it, so a worktree tests its own copy, and\nreaches the Windows branches through the module flags and pure functions they key on:\n\n  - shell-command-scan.powershell_as_posix writes, for a PowerShell command, the POSIX\n    command the Bash guards read, and write_targets finds the PowerShell writes in it;\n  - hook-dispatch hands a PowerShell call to the Bash guards and drops their rewrites for it;\n  - home-settings-sync leaves out iterm-tab-status on Windows only;\n  - tree_copy copies and prunes the way rsync -rlt --delete does when rsync is absent;\n  - the interpreter rule knows a Windows python3.14.exe.\n\nThe controls: a Bash call keeps its rewrites, a non-Windows home keeps iterm-tab-status, and a\nplain command passes through the translator unchanged.\n\nRun: python3 ~/.agent-context/global/scripts/test-windows-native.py'
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    if spec is None or spec.loader is None:
        raise ImportError(filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scs = load("scs", "shell-command-scan.py")
dispatch = load("hook_dispatch", "hook-dispatch.py")
tree_copy = load("tree_copy", "tree_copy.py")
hp = load("harness_paths", "harness_paths.py")

fails, ran = [], []


def check(name, got, want):
    ran.append(name)
    if got != want:
        fails.append("%s\n    got:  %r\n    want: %r" % (name, got, want))



P = scs.powershell_as_posix
check("git passes through unchanged", P("git reset --hard HEAD"), "git reset --hard HEAD")
check("a recursive Remove-Item is a recursive rm", P("Remove-Item -Recurse -Force Kit"), "rm -rf Kit")
check("the rm alias with -r abbreviated", P(r"rm -r -fo C:\Users\u\X"), "rm -rf C:/Users/u/X")
check("a plain Remove-Item is a plain rm", P("Remove-Item a.txt"), "rm a.txt")
check("a comma list names two paths", P(r"Remove-Item -Path C:\a,C:\b -r"), "rm -rf C:/a C:/b")
check("Set-Content writes its -Path, not its -Value",
      P(r"Set-Content -Path C:\t\p.txt -Value hello"), "tee C:/t/p.txt")
check("Out-File writes its first positional", P(r"Out-File C:\x\y.txt"), "tee C:/x/y.txt")
check("New-Item -ItemType Directory makes a directory",
      P(r"New-Item -ItemType Directory -Path .agents\tmp\x"), "mkdir .agents/tmp/x")
check("Copy-Item -Destination is cp's last word",
      P(r"Copy-Item a.txt -Destination C:\b.txt"), "cp a.txt C:/b.txt")
check("Move-Item positional is mv", P("Move-Item a b"), "mv a b")
check("a redirect keeps its target", P(r"'x' > C:\ProgramData\p.txt"), "'x' > C:/ProgramData/p.txt")
check("a stream merge is no file", P("git status 2>&1 | Out-Null"), "git status | out-null")
check("a quoted exe path runs by its name",
      P(r'& "C:\Program Files\Git\cmd\git.exe" stash'), "git stash")
check("a backtick continuation joins the line",
      P("git log --oneline `\n  --stat"), "git log --oneline --stat")
check("Set-Location is cd", P(r"Set-Location C:\r; git commit -m x"), "cd C:/r ; git commit -m x")
check("a quoted word stays data", P("git commit -m 'git stash is fine'"),
      "git commit -m 'git stash is fine'")
check("$env:NAME reads as $NAME", P(r"Remove-Item -Recurse $env:TEMP\x"), 'rm -rf "${TEMP}/x"')
check("a script block's statements are separate",
      P("Get-ChildItem | ForEach-Object { Remove-Item $_ -Recurse }"),
      "get-childitem | foreach-object ; rm -rf $_ ;")
check("a command name is matched without case", P("GIT reset --hard"), "git reset --hard")
check("cmd's rd /s is a recursive rm", P(r"rd /s /q C:\x"), "rm -rf C:/x")
check("cmd's del without /s is a plain rm", P(r"del /q a.txt"), "rm a.txt")
check("iex hands its string to the guards", P('iex "git reset --hard HEAD"'), "git reset --hard HEAD")
check("pwsh -c hands its command to the guards", P("pwsh -NoProfile -c 'git stash'"), "git stash")
check("pwsh -EncodedCommand is decoded",
      P("powershell -enc " + __import__("base64").b64encode("git clean -fdx".encode("utf-16-le")).decode()),
      "git clean -fdx")
check("cmd /c hands its command to the guards", P("cmd /c git reset --hard"), "git reset --hard")
check("pwsh -ExecutionPolicy is no command", P("pwsh -ExecutionPolicy Bypass -File x.ps1"),
      "pwsh -ExecutionPolicy Bypass -File x.ps1")

base = tempfile.mkdtemp(prefix="test-windows-native.")
try:
    os.makedirs(os.path.join(base, "d"))
    got = scs.write_targets(P(r"Set-Content -Path d\f.txt -Value 1"), base)
    check("write_targets sees a Set-Content target", got, [os.path.join(base, "d", "f.txt")])
    got = scs.write_targets(P(r"Copy-Item x -Destination d\y"), base)
    check("write_targets sees a Copy-Item destination", got, [os.path.join(base, "d", "y")])

    
    src, dst = os.path.join(base, "src"), os.path.join(base, "dst")
    os.makedirs(os.path.join(src, "skill"))
    for rel in ("skill/SKILL.md", "skill/x.meta.toml", "top.md"):
        with open(os.path.join(src, rel), "w") as fh:
            fh.write(rel)
    os.makedirs(os.path.join(dst, "gone"))
    for rel in ("stale.md", "kept.meta.toml"):
        with open(os.path.join(dst, rel), "w") as fh:
            fh.write(rel)
    real_which = shutil.which
    shutil.which = lambda name: None if name == "rsync" else real_which(name)
    try:
        ok = tree_copy.copy_tree(src, dst, delete=True)
    finally:
        shutil.which = real_which
    listing = sorted(os.path.relpath(os.path.join(dp, f), dst)
                     for dp, _dn, files in os.walk(dst) for f in files)
    check("tree_copy without rsync copies, prunes, and leaves excluded files alone",
          (ok, listing), ((True, ""), ["kept.meta.toml", "skill/SKILL.md", "top.md"]))
finally:
    shutil.rmtree(base, ignore_errors=True)


call = {"tool_name": "PowerShell", "tool_input": {"command": "Remove-Item -Recurse Kit",
                                                   "description": "d"}, "cwd": "C:/r"}
got = dispatch.as_shell_call(call)
check("a PowerShell call reaches the guards as Bash",
      (got["tool_name"], got["tool_input"]["command"], got["shell"], got["shell_command"],
       got["tool_input"]["description"]),
      ("Bash", "rm -rf Kit", "powershell", "Remove-Item -Recurse Kit", "d"))
check("the agent's own payload is not changed", call["tool_input"]["command"],
      "Remove-Item -Recurse Kit")
bash = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
check("a Bash call is passed through as is", dispatch.as_shell_call(bash) is bash, True)


def outcome(out):
    o = dispatch.Outcome("g.py")
    o.out = json.dumps(out)
    return o


rewrite = outcome({"hookSpecificOutput": {"permissionDecision": "allow",
                                          "updatedInput": {"command": "ls /abs"}}})
_, out, _ = dispatch.merge("PreToolUse", [rewrite], [], rewrites=False)
check("a translated call gets no rewrite",
      "updatedInput" in json.loads(out).get("hookSpecificOutput", {}), False)
_, out, _ = dispatch.merge("PreToolUse", [rewrite], [])
check("a Bash call keeps its rewrite",
      json.loads(out)["hookSpecificOutput"]["updatedInput"], {"command": "ls /abs"})


sync = load("home_settings_sync", "home-settings-sync.py")
check("iterm-tab-status is left out on Windows",
      sync.runs_here(os.path.join(sync.H, "iterm-tab-status.py"), windows=True), False)
check("and kept everywhere else",
      sync.runs_here(os.path.join(sync.H, "iterm-tab-status.py"), windows=False), True)
check("a guard runs on Windows", sync.runs_here(os.path.join(sync.H, "guard-git-write.py"),
                                                windows=True), True)
check("python3.14.exe is an interpreter", hp.is_interpreter("C:/u/.local/bin/python3.14.exe"), True)
check("a script is not", hp.is_interpreter("C:/u/hook-dispatch.py"), False)




hooks_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
uname_hooks = sorted(n for n in os.listdir(hooks_dir) if n.endswith(".py")
                     and "os.uname()" in open(os.path.join(hooks_dir, n), encoding="utf-8").read())
check("no hook calls os.uname", uname_hooks, [])

print("%d/%d passed" % (len(ran) - len(fails), len(ran)))
if fails:
    print("FAILED:\n  " + "\n  ".join(fails))
    sys.exit(1)
