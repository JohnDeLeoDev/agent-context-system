'A store commit runs its hooks with the server-task marker set.\n\nThe commit hook runs store checks that forward themselves to the daemon unless the\nmarker is present, and the daemon is the process waiting on the commit.'
import os
import stat
import subprocess

from fixture_signing import signing_config_beside

from agent_context.store import ContextStore
from agent_context.store_tasks import SERVER_TASK_ENV


def _git(cwd, *args):
    return subprocess.run(("git", "-C", str(cwd), *args), capture_output=True,
                          text=True, timeout=60, check=True)


def _repo_with_hook(tmp_path):
    root = tmp_path / "ctx"
    (root / "global").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    for key, value in signing_config_beside(root):
        _git(root, "config", key, value)
    record = tmp_path / "seen.txt"
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text(f'#!/bin/sh\nprintf "%s" "${{{SERVER_TASK_ENV}-unset}}" > "{record}"\n')
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    return ContextStore(root=str(root)), root, record


def test_store_commit_hook_sees_server_task_marker(tmp_path, monkeypatch):
    monkeypatch.delenv(SERVER_TASK_ENV, raising=False)
    store, root, record = _repo_with_hook(tmp_path)
    (root / "a.txt").write_text("x")
    _git(root, "add", "a.txt")
    proc = store._commit_staged("test commit")
    assert proc.returncode == 0, proc.stderr
    assert record.read_text() == "1"


def test_non_commit_git_call_adds_no_marker(tmp_path, monkeypatch):
    monkeypatch.delenv(SERVER_TASK_ENV, raising=False)
    store, root, _record = _repo_with_hook(tmp_path)
    seen = {}
    real_run = subprocess.run

    def spy(cmd, *a, **kw):
        seen["env"] = kw.get("env")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    store._git("status", "--short")
    assert seen["env"] is None
    assert SERVER_TASK_ENV not in os.environ
