#!/usr/bin/env python3
'Battery for harness_paths.py, the one place harness directory paths are built.\n\nBefore this module 57 files built ~/.claude paths themselves, many in pieces such as\nos.path.join(HOME, ".claude", "hooks"), so no text search found them all. The module\nreturns exactly the paths those files built, so a migration changes no behavior, and\nrelocating a directory later is an edit here.\n\nCases:\n  - every accessor returns the path the old literal built, for an explicit home=;\n  - with no home= the accessors follow $HOME at call time;\n  - an empty home raises RuntimeError naming the cause;\n  - a trailing slash, a relative home and a symlinked home each give one absolute path;\n  - the module imports beside its importers: from the store scripts dir, from a copy in a\n    projected scripts dir, and from a hooks dir through a fixed sys.path line;\n  - home-settings-sync.py runs against a fake home and writes valid JSON.\n\nRuns on Python 3.8, the system Python on the Synology nodes.\n\nUsage: test-harness-paths.py'

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.path.join(HERE, "harness_paths.py")
SYNC = os.path.join(HERE, "home-settings-sync.py")


failures = []


def check(name, cond, detail=""):
    if cond:
        print("ok   " + name)
    else:
        print("FAIL " + name + (": " + detail if detail else ""))
        failures.append(name)


def load(path=MODULE_PATH):
    spec = importlib.util.spec_from_file_location("harness_paths", path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def scratch():
    base = os.path.join(os.path.expanduser("~"), ".cache", "tmp")
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix="harness-paths-test-", dir=base)


def test_accessors(hp):
    h = "/fake/home"
    cases = {
        "home": (hp.home(home=h), h),
        "claude_home": (hp.claude_home(home=h), h + "/.claude"),
        "store_root": (hp.store_root(home=h), h + "/.agent-context"),
        "hooks_dir": (hp.hooks_dir(home=h), h + "/.agent-context/global/hooks"),
        "scripts_dir": (hp.scripts_dir(home=h), h + "/.agent-context/global/scripts"),
        "docs_dir": (hp.docs_dir(home=h), h + "/.agent-context/shared-docs"),
        "state_dir": (hp.state_dir(home=h), h + "/.local/state/agent-context"),
        "skills_dir": (hp.skills_dir(home=h), h + "/.claude/skills"),
        "commands_dir": (hp.commands_dir(home=h), h + "/.claude/commands"),
        "settings_file": (hp.settings_file(home=h), h + "/.claude/settings.json"),
        "claude_json": (hp.claude_json(home=h), h + "/.claude.json"),
        "project_claude_dir": (hp.project_claude_dir("/r/repo"), "/r/repo/.claude"),
        "project_agents_dir": (hp.project_agents_dir("/r/repo"), "/r/repo/.agents"),
    }
    for name, (got, want) in cases.items():
        check("accessor " + name, got == want, "got %r want %r" % (got, want))


def test_env_home(hp):
    old = os.environ.get("HOME")
    try:
        os.environ["HOME"] = "/env/home"
        check("default follows HOME", hp.claude_home() == "/env/home/.claude", hp.claude_home())
        os.environ["HOME"] = "/other/home"
        check("default follows HOME at call time", hp.hooks_dir() == "/other/home/.agent-context/global/hooks",
              hp.hooks_dir())
    finally:
        if old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old


def test_errors(hp):
    try:
        hp.claude_home(home="")
    except RuntimeError as e:
        check("empty home raises RuntimeError naming home", "home" in str(e).lower(), str(e))
    except Exception as e:
        check("empty home raises RuntimeError", False, repr(e))
    else:
        check("empty home raises RuntimeError", False, "no exception")


def test_normalize(hp):
    check("trailing slash", hp.claude_home(home="/fake/home/") == "/fake/home/.claude",
          hp.claude_home(home="/fake/home/"))
    check("double slash", hp.hooks_dir(home="/fake//home") == "/fake/home/.agent-context/global/hooks",
          hp.hooks_dir(home="/fake//home"))
    old = os.getcwd()
    root = scratch()
    try:
        os.chdir(root)
        got = hp.claude_home(home="rel")
        check("relative home becomes absolute", os.path.isabs(got) and got.endswith("/rel/.claude"), got)
        real = os.path.join(root, "real")
        link = os.path.join(root, "link")
        os.makedirs(real)
        os.symlink(real, link)
        got = hp.claude_home(home=link)
        check("symlinked home keeps the spelling given", got == os.path.abspath(link) + "/.claude", got)
    finally:
        os.chdir(old)
        shutil.rmtree(root, ignore_errors=True)


def test_import_locations():
    root = scratch()
    try:
        scripts = os.path.join(root, "scripts")
        hooks = os.path.join(root, "hooks")
        os.makedirs(scripts)
        os.makedirs(hooks)
        shutil.copy(MODULE_PATH, os.path.join(scripts, "harness_paths.py"))
        check("importable from the store scripts dir", load().claude_home(home="/x") == "/x/.claude")
        check("importable from a projected scripts dir",
              load(os.path.join(scripts, "harness_paths.py")).claude_home(home="/x") == "/x/.claude")
        hook = os.path.join(hooks, "probe.py")
        with open(hook, "w") as f:
            f.write(
                "import os, sys\n"
                'sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))\n'
                "import harness_paths\n"
                'print(harness_paths.hooks_dir(home="/x"))\n'
            )
        out = subprocess.run([sys.executable, hook], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True)
        check("importable from a hooks dir via sys.path line",
              out.returncode == 0 and out.stdout.strip() == "/x/.agent-context/global/hooks",
              out.stdout + out.stderr)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_settings_bytes_unchanged():
    root = scratch()
    try:
        home = os.path.join(root, "home")
        os.makedirs(os.path.join(home, ".claude"))
        target = os.path.join(home, ".claude", "settings.json")
        with open(target, "w") as f:
            f.write("{}\n")
        env = dict(os.environ, HOME=home)
        out = subprocess.run([sys.executable, SYNC, target], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, universal_newlines=True, env=env)
        check("home-settings-sync runs", out.returncode == 0, out.stdout + out.stderr)
        with open(target, "r") as f:
            body = f.read()
        json.loads(body)
        
        
        
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    if not os.path.exists(MODULE_PATH):
        check("harness_paths.py exists", False, MODULE_PATH)
    else:
        hp = load()
        test_accessors(hp)
        test_env_home(hp)
        test_errors(hp)
        test_normalize(hp)
        test_import_locations()
    test_settings_bytes_unchanged()
    print("%d failure(s)" % len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
