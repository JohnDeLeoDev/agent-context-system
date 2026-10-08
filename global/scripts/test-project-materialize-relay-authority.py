#!/usr/bin/env python3
'Focused regression checks for relay-authoritative project materialization.'
import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile


SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "project-materialize.py")
UUID = "11111111-1111-4111-8111-111111111111"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def run(root, mode, *, use_tmpdir=True, check=True, legacy=False):
    home = os.path.join(root, "home")
    store = os.path.join(home, ".agent-context")
    repo = os.path.join(home, "Demo")
    hook = os.path.join(root, "hook")
    write(os.path.join(store, ".git", "keep"), "stale checkout\n")
    write(os.path.join(store, "projects", "Stale", "project.toml"), 'display_name = "stale"\n')
    write(os.path.join(store, "projects", "Stale", "skills", "old", "SKILL.md"), "stale\n")
    legacy_before = None
    if legacy:
        key = "projects/Stale/skills/old/SKILL.md"
        old = os.path.join(store, key)
        with open(old, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        record = os.path.join(home, ".cache", "agent-context", "project-scope", "stale.json")
        write(record, json.dumps({"keys": [key], "digests": {key: digest}}))
        with open(old, "rb") as fh:
            old_before = fh.read()
        with open(record, "rb") as fh:
            legacy_before = (old_before, fh.read())
    os.makedirs(repo)
    git("init", "-q", cwd=repo)
    git("remote", "add", "origin", "git@github.com:org/demo.git", cwd=repo)
    write(os.path.join(repo, ".agents", "project-id"),
          'id = "%s"\ndisplay_name = "Demo"\n' % UUID)
    bundle = {
        "projects/Demo/project.toml": 'display_name = "canonical"\n',
        "projects/Demo/skills/current/SKILL.md": "canonical\n",
    }
    fake = '''
import json, os, sys
sys.path.insert(0, os.environ["SCRIPT_DIR"])
import store_mcp
def relay_env():
    return {"AGENT_CONTEXT_HOST": "relay", "AGENT_CONTEXT_TOKEN": "token"}
def call(name, arguments, env=None):
    with open(os.environ["CALL_LOG"], "w", encoding="utf-8") as fh:
        json.dump({"name": name, "arguments": arguments}, fh)
    if os.environ["FETCH_MODE"] == "fail":
        raise store_mcp.StoreUnreachable("offline")
    return json.loads(os.environ["BUNDLE"])
store_mcp.relay_env = relay_env
store_mcp.call = call
'''
    write(os.path.join(hook, "sitecustomize.py"), fake)
    env = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "PYTHONPATH": hook, "AGENT_CONTEXT_STORE": store,
           "CALL_LOG": os.path.join(root, "call.json"), "FETCH_MODE": mode,
           "BUNDLE": json.dumps(bundle), "GIT_CONFIG_NOSYSTEM": "1",
           "SCRIPT_DIR": os.path.dirname(SCRIPT)}
    if use_tmpdir:
        os.makedirs(os.path.join(root, "tmp"))
        env["TMPDIR"] = os.path.join(root, "tmp")
    before = open(os.path.join(repo, ".agents", "project-id"), encoding="utf-8").read()
    args = [sys.executable, SCRIPT] + (["--check"] if check else []) + [repo]
    proc = subprocess.run(args, env=env,
                          capture_output=True, text=True, timeout=30)
    assert os.path.isfile(env["CALL_LOG"]), proc.stdout + proc.stderr
    with open(env["CALL_LOG"], encoding="utf-8") as fh:
        call = json.load(fh)
    after = open(os.path.join(repo, ".agents", "project-id"), encoding="utf-8").read()
    legacy_unchanged = True
    if legacy_before is not None:
        with open(old, "rb") as fh:
            old_after = fh.read()
        with open(record, "rb") as fh:
            record_after = fh.read()
        legacy_unchanged = (old_after, record_after) == legacy_before
    return (proc, call, before == after, not os.path.exists(os.path.join(repo, ".agents", "tmp")),
            legacy_unchanged, repo)


def main():
    root = tempfile.mkdtemp(prefix="project-materialize-relay-")
    try:
        proc, call, unchanged, _scratch_clean, _legacy_unchanged, _repo = run(root, "ok")
        assert proc.returncode == 3, proc.stdout + proc.stderr
        assert call["name"] == "get_materialized", call
        assert call["arguments"]["project"] == [UUID], call
        assert "store file(s) to add/update" in proc.stdout, proc.stdout
        assert "matches no store project" not in proc.stdout, proc.stdout
        assert unchanged
    finally:
        shutil.rmtree(root, ignore_errors=True)
    root = tempfile.mkdtemp(prefix="project-materialize-relay-")
    try:
        proc, call, unchanged, _scratch_clean, _legacy_unchanged, _repo = run(root, "fail")
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert call["name"] == "get_materialized", call
        assert "projection unchanged" in proc.stderr, proc.stderr
        assert unchanged
    finally:
        shutil.rmtree(root, ignore_errors=True)
    root = tempfile.mkdtemp(prefix="project-materialize-relay-")
    try:
        proc, call, unchanged, scratch_clean, _legacy_unchanged, _repo = run(root, "ok", use_tmpdir=False)
        assert proc.returncode == 3, proc.stdout + proc.stderr
        assert call["name"] == "get_materialized", call
        assert unchanged
        assert scratch_clean, "project .agents/tmp was left behind"
    finally:
        shutil.rmtree(root, ignore_errors=True)
    root = tempfile.mkdtemp(prefix="project-materialize-relay-")
    try:
        proc, call, _unchanged, _scratch_clean, legacy_unchanged, repo = run(
            root, "ok", check=False, legacy=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert call["name"] == "get_materialized", call
        assert legacy_unchanged, "stale checkout source or matching manifest was retired"
        assert os.path.isfile(os.path.join(repo, ".agents", "claude", "skills", "current", "SKILL.md"))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("project-materialize relay authority: passed")


if __name__ == "__main__":
    main()
