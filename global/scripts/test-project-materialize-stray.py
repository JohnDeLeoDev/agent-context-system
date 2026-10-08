#!/usr/bin/env python3
"A harness directory inside a tree git ignores, or at the root of a nested repository,\nis not this repo's and is not a stray. One in a tracked subdirectory still is.\n\n    python3 test-project-materialize-stray.py"
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def load():
    spec = importlib.util.spec_from_file_location(
        "project_materialize", os.path.join(HERE, "project-materialize.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def put(root, rel):
    full = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write("x\n")


def repo(root, ignore):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    subprocess.run(["git", "init", "-q", root], check=True, env=env)
    with open(os.path.join(root, ".gitignore"), "w", encoding="utf-8") as f:
        f.write(ignore)
    put(root, ".claude/skills/a.md")       
    put(root, ".agents/skills/a.md")


def strays(mod, root):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        clean = mod.report_stray_harness_dirs(root)
    return clean, out.getvalue()


def test_an_ignored_tree_and_a_nested_repository_are_not_strays(mod, root):
    repo(root, ".ci/\n.agents/tmp/\n")
    put(root, ".ci/checkout/.claude/settings.json")
    put(root, ".agents/tmp/run1/.agents/skills/a.md")
    os.makedirs(os.path.join(root, "Helper", ".git"))
    put(root, "Helper/.agents/project-id")
    put(root, "Helper/.claude/settings.json")
    clean, text = strays(mod, root)
    assert clean and "STRAY" not in text, text


def test_a_harness_dir_in_a_tracked_subdirectory_is_still_a_stray(mod, root):
    repo(root, ".ci/\n")
    put(root, "packages/web/.claude/settings.json")
    clean, text = strays(mod, root)
    assert not clean and "packages/web/.claude" in text, text


def test_without_git_every_nested_harness_dir_is_reported(mod, root):
    put(root, ".claude/skills/a.md")
    put(root, ".ci/checkout/.claude/settings.json")
    clean, text = strays(mod, root)
    assert not clean and ".ci/checkout/.claude" in text, text


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    mod = load()
    for name, fn in tests:
        with tempfile.TemporaryDirectory() as root:
            root = os.path.realpath(root)
            try:
                fn(mod, root)
                print(f"ok    {name}")
            except Exception as e:
                failed += 1
                print(f"FAIL  {name}: {e}")
    print(f"{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
