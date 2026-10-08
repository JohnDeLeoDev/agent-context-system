#!/usr/bin/env python3
"Runs the real global/scripts/*.py from the store against fixture HOME/store/repo trees\nbuilt under a tempdir inside $HOME/.cache/tmp, invoked exactly as their callers do (argv,\nenv, stdin). Same PASS/FAIL print style and exit code as test-project-settings-sync.py.\n\nLocked parity case files (script-port-cases-b1..b4.py) pin the shell original's behavior\nfor some of these same scenarios. Those pins are now WRONG BY DESIGN for three of them --\nsee the delivery report for the exact case names -- and are not edited here."
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

STORE = os.path.expanduser("~/.agent-context")
SCRIPTS = os.path.join(STORE, "global", "scripts")
TMP_BASE = os.path.join(os.path.expanduser("~"), ".cache", "tmp")

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


def mkdtemp(prefix):
    os.makedirs(TMP_BASE, exist_ok=True)
    return tempfile.mkdtemp(dir=TMP_BASE, prefix=prefix)


def bin_dir_without(*excluded):
    'A PATH directory holding symlinks to every real tool this battery needs, except\n    the named ones -- so a fixture run can prove a script no longer depends on them.'
    d = mkdtemp("bin-")
    for tool in ("git", "bash", "sh", "python3", "uname"):
        if tool in excluded:
            continue
        real = shutil.which(tool)
        if real:
            os.symlink(real, os.path.join(d, tool))
    return d


def git_repo(path, commit=True):
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q", path], check=True)
    if commit:
        subprocess.run(["git", "-C", path, "-c", "user.email=t@t.t", "-c", "user.name=t",
                         "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    return path


def run(script, args, env=None, cwd=None, input_text=None):
    full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if env:
        full_env.update(env)
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, script)] + args,
                           env=full_env, cwd=cwd, input=input_text,
                           capture_output=True, text=True)


print("global-script-fixes: bug battery (user's ruling, 2026-09-14)")






print("\n[ralph-guard.py] dead friendly messages must become reachable")


work = mkdtemp("rg1a-")
home = os.path.join(work, "home")
os.makedirs(home, exist_ok=True)
repo = git_repo(os.path.join(work, "repo"))
p = run("ralph-guard.py", [], env={"HOME": home}, cwd=repo)
check("no plugin installed: exits 1", p.returncode == 1, "rc=%r" % p.returncode)
check("no plugin installed: prints the plugin-not-installed message",
      "ralph-loop plugin not installed" in p.stderr, "stderr=%r" % p.stderr)


work = mkdtemp("rg1b-")
home = os.path.join(work, "home")
plugin_dir = os.path.join(home, ".claude/plugins/cache/claude-plugins-official/"
                                 "ralph-loop/1.0.0/hooks")
os.makedirs(plugin_dir, exist_ok=True)
with open(os.path.join(plugin_dir, "stop-hook.sh"), "w", encoding="utf-8") as fh:
    fh.write("#!/bin/bash\necho '{}'\n")
notrepo = os.path.join(work, "notrepo")
os.makedirs(notrepo, exist_ok=True)
p = run("ralph-guard.py", [], env={"HOME": home}, cwd=notrepo)
check("not a git repo: exits 1 (not git's raw 128)", p.returncode == 1, "rc=%r" % p.returncode)
check("not a git repo: prints the not-in-a-git-repository message",
      "not in a git repository" in p.stderr, "stderr=%r" % p.stderr)



work = mkdtemp("rg1c-")
home = os.path.join(work, "home")
plugin_dir = os.path.join(home, ".claude/plugins/cache/claude-plugins-official/"
                                 "ralph-loop/1.0.0/hooks")
os.makedirs(plugin_dir, exist_ok=True)
with open(os.path.join(plugin_dir, "stop-hook.sh"), "w", encoding="utf-8") as fh:
    fh.write("#!/bin/bash\necho '{}'\n")
repo = git_repo(os.path.join(work, "repo"))
state_dir = os.path.join(repo, ".claude")
os.makedirs(state_dir, exist_ok=True)
with open(os.path.join(state_dir, "ralph-loop.local.md"), "w", encoding="utf-8") as fh:
    fh.write("---\niteration: 1\nmax_iterations: 5\n---\nbody\n")
p = run("ralph-guard.py", [], env={"HOME": home}, cwd=repo)
check("session id unresolvable: exits 1", p.returncode == 1, "rc=%r" % p.returncode)
check("session id unresolvable: prints the cannot-resolve message",
      "cannot resolve this session" in p.stderr, "stderr=%r" % p.stderr)








print("\n[wt-sweep.py] symlinked husks are never removed; a bad --main path fails cleanly")


work = mkdtemp("wts2a-")
main = git_repo(os.path.join(work, "main"))
wt_dir = os.path.join(main, ".claude", "worktrees")
os.makedirs(wt_dir, exist_ok=True)
outside = os.path.join(work, "outside-empty")
os.makedirs(outside, exist_ok=True)
ghost = os.path.join(wt_dir, "ghost")
os.symlink(outside, ghost)
p = run("wt-sweep.py", [main])
check("symlinked husk: sweep still exits 0", p.returncode == 0, "rc=%r" % p.returncode)
check("symlinked husk: the symlink is never removed", os.path.islink(ghost),
      "islink=%r stdout=%r" % (os.path.islink(ghost), p.stdout))


work = mkdtemp("wts2b-")
main = git_repo(os.path.join(work, "main"))
wt_dir = os.path.join(main, ".claude", "worktrees")
husk = os.path.join(wt_dir, "husk1")
os.makedirs(husk, exist_ok=True)
p = run("wt-sweep.py", [main])
check("real empty husk: still removed (not over-corrected)", not os.path.isdir(husk),
      "still exists=%r stdout=%r" % (os.path.isdir(husk), p.stdout))


work = mkdtemp("wts2c-")
notrepo = os.path.join(work, "notrepo")
os.makedirs(os.path.join(notrepo, ".claude", "worktrees", "husk"), exist_ok=True)
p = run("wt-sweep.py", [notrepo])
check("non-repo main dir: does not leak git's raw 128", p.returncode != 128,
      "rc=%r" % p.returncode)
check("non-repo main dir: exits with a defined nonzero code", p.returncode == 1,
      "rc=%r" % p.returncode)
check("non-repo main dir: prints a clear message", "not a git repository" in p.stderr,
      "stderr=%r" % p.stderr)









print("\n[project-materialize.py] --check writes nothing")

work = mkdtemp("pm3-")
home = os.path.join(work, "home")
os.makedirs(home, exist_ok=True)
store = os.path.join(work, "store")
os.makedirs(os.path.join(store, "projects", "testproj"), exist_ok=True)
with open(os.path.join(store, "projects", "testproj", "project.toml"), "w",
          encoding="utf-8") as fh:
    fh.write('uuid = "abc-123-uuid"\ndisplay_name = "Test Project"\n')
directory = os.path.join(work, "testproj")
os.makedirs(directory, exist_ok=True)
marker = os.path.join(directory, ".agents", "project-id")

p = run("project-materialize.py", ["--check", directory],
        env={"HOME": home, "AGENT_CONTEXT_STORE": store})
check("--check does not write .agents/project-id", not os.path.exists(marker),
      "exists=%r stdout=%r" % (os.path.exists(marker), p.stdout))


p2 = run("project-materialize.py", [directory],
         env={"HOME": home, "AGENT_CONTEXT_STORE": store})
check("a normal apply run still writes .agents/project-id", os.path.exists(marker),
      "exists=%r stdout=%r" % (os.path.exists(marker), p2.stdout))




print("\n[verify-fleet.py] --hosts with no value is refused")

work = mkdtemp("vf4-")
store = os.path.join(work, "store")
os.makedirs(os.path.join(store, "server", "tests"), exist_ok=True)
p = run("verify-fleet.py", ["--hosts"], env={"AGENT_CONTEXT_STORE": store})
check("--hosts with no value: not the misleading exit 2 (no verifier reachable)",
      p.returncode != 2, "rc=%r stderr=%r" % (p.returncode, p.stderr))
check("--hosts with no value: exits with the usage-error code (64)",
      p.returncode == 64, "rc=%r" % p.returncode)
check("--hosts with no value: names --hosts in the error",
      "--hosts" in p.stderr, "stderr=%r" % p.stderr)





print("\n[statusline-command.py] no jq dependency; a real HOME path boundary")

nojq_bin = bin_dir_without("jq")
home = mkdtemp("sl5-home-")
sibling = os.path.join(home + "brew-project")  
os.makedirs(sibling, exist_ok=True)
payload = json.dumps({"cwd": sibling, "model": {"display_name": "Sonnet"},
                       "context_window": {"used_percentage": 10}})
p = run("statusline-command.py", [], env={"HOME": home, "PATH": nojq_bin},
        input_text=payload)
check("runs correctly with no jq anywhere on PATH", p.returncode == 0,
      "rc=%r stderr=%r" % (p.returncode, p.stderr))
check("a sibling directory that merely starts with $HOME is not mangled into a tilde",
      "~brew-project" not in p.stdout and sibling in p.stdout,
      "stdout=%r" % p.stdout)


home2 = mkdtemp("sl5-home2-")
child = os.path.join(home2, "project")
os.makedirs(child, exist_ok=True)
payload2 = json.dumps({"cwd": child})
p2 = run("statusline-command.py", [], env={"HOME": home2, "PATH": nojq_bin},
          input_text=payload2)
check("a genuine child of $HOME is still abbreviated to a tilde",
      "~/project" in p2.stdout or "~" + os.sep + "project" in p2.stdout,
      "stdout=%r" % p2.stdout)





print("\n[release-server.py] falls back to the hostname when chezmoi.toml is missing")


def load_release_server():
    path = os.path.join(SCRIPTS, "release-server.py")
    spec = importlib.util.spec_from_file_location("release_server_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    rs = load_release_server()
    home_no_chezmoi = mkdtemp("rs6-home-")
    who = rs.resolve_who(home_no_chezmoi)
    check("resolve_who falls back to a non-empty hostname, not exit(2), "
          "when chezmoi.toml is absent", isinstance(who, str) and who != "",
          "got %r" % (who,))

    home_with_chezmoi = mkdtemp("rs6-home2-")
    cfgdir = os.path.join(home_with_chezmoi, ".config", "chezmoi")
    os.makedirs(cfgdir, exist_ok=True)
    with open(os.path.join(cfgdir, "chezmoi.toml"), "w", encoding="utf-8") as fh:
        fh.write('[data]\n  machine_id = "testmachine"\n')
    who2 = rs.resolve_who(home_with_chezmoi)
    check("resolve_who still reads machine_id when chezmoi.toml is present",
          who2 == "testmachine", "got %r" % (who2,))
except Exception as exc:  
    check("release-server.py exposes a resolve_who() that falls back instead of aborting",
          False, "%s: %s" % (type(exc).__name__, exc))





print("\n[invariant-check.py] no-unsigned-commit-fallback never scans its own file")
try:
    ic_spec = importlib.util.spec_from_file_location(
        "invariant_check_under_test", os.path.join(SCRIPTS, "invariant-check.py"))
    assert ic_spec is not None and ic_spec.loader is not None
    ic = importlib.util.module_from_spec(ic_spec)
    ic_spec.loader.exec_module(ic)
    unsigned_inv = next(i for i in ic.REGISTRY if i.id == "no-unsigned-commit-fallback")
    scanned = {os.path.basename(p) for p in unsigned_inv.sites()}
    check("the invariant skips invariant-check.py, which carries the pattern as text",
          "invariant-check.py" not in scanned, "invariant-check.py is a site")
except Exception as exc:
    check("the no-unsigned-commit-fallback self-exclusion is checkable", False,
          "%s: %s" % (type(exc).__name__, exc))





print("\nglobal-script-fixes: %d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
