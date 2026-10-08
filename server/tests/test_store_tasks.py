'A server task is a store script the daemon runs for any caller; what it changes is a daemon\nwrite, so it lands in the ledger and is committed. Everything else dirty in the tree is an\noutside edit (Obsidian, an editor): reported, never committed.'
from __future__ import annotations

import asyncio
import json
import os
import subprocess

import pytest
from fixture_signing import signing_config_beside

from agent_context import paths, server, store_tasks, write_ledger
from agent_context.store import ContextStore


def _git(cwd, *args):
    return subprocess.run(("git", "-C", str(cwd), *args), capture_output=True, text=True,
                          check=True)


@pytest.fixture
def repo_store(tmp_path, monkeypatch):
    root = tmp_path / "ctx"
    (root / "global" / "scripts").mkdir(parents=True)
    (root / "global" / "memory").mkdir(parents=True)
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    for k, v in signing_config_beside(root):
        _git(root, "config", k, v)
    (root / "global" / ".keep").write_text("")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    return ContextStore(root=str(root))


def _task(monkeypatch, root, name, body, timeout=30):
    (root / "global" / "scripts" / f"{name}.py").write_text(body)
    monkeypatch.setitem(store_tasks.TASKS, name, store_tasks.Task(timeout))
    monkeypatch.setitem(store_tasks._RUNNING, name, store_tasks.threading.Lock())


PROBE = """import os, sys
print("server-task=" + os.environ.get("AGENT_CONTEXT_SERVER_TASK", ""))
print("cwd=" + os.getcwd())
print("args=" + " ".join(sys.argv[1:]))
print("stdin=" + sys.stdin.read())
if "--write" in sys.argv:
    with open(os.path.join(os.environ["AGENT_CONTEXT_STORE"], "global", "memory", "t.md"), "w") as fh:
        fh.write("from a task\\n")
sys.stderr.write("to stderr\\n")
sys.exit(int(os.environ.get("PROBE_EXIT", "0")))
"""


def test_unknown_task_is_refused(repo_store):
    out = store_tasks.run(repo_store, "rm-everything", [])
    assert "unknown task" in out["error"]


def test_args_must_be_strings(repo_store, monkeypatch, tmp_path):
    _task(monkeypatch, tmp_path / "ctx", "probe", PROBE)
    assert "list of strings" in store_tasks.run(repo_store, "probe", [1])["error"]


def test_a_task_runs_as_a_server_task_with_its_args_and_stdin(repo_store, monkeypatch, tmp_path):
    root = tmp_path / "ctx"
    _task(monkeypatch, root, "probe", PROBE)
    out = store_tasks.run(repo_store, "probe", ["--flag", "x y"], stdin="hello")
    assert out["exit"] == 0, out
    assert "server-task=1" in out["stdout"]
    assert "args=--flag x y" in out["stdout"]
    assert "stdin=hello" in out["stdout"]
    assert out["stderr"] == "to stderr\n"
    assert f"cwd={root}" in out["stdout"]            
    assert out["changed"] == []


def test_the_caller_cwd_is_used_when_it_exists_here(repo_store, monkeypatch, tmp_path):
    _task(monkeypatch, tmp_path / "ctx", "probe", PROBE)
    here = tmp_path / "caller"
    here.mkdir()
    assert f"cwd={here}" in store_tasks.run(repo_store, "probe", [], cwd=str(here))["stdout"]
    gone = str(tmp_path / "on-another-machine")
    assert f"cwd={tmp_path / 'ctx'}" in store_tasks.run(repo_store, "probe", [], cwd=gone)["stdout"]


def test_exit_code_is_relayed(repo_store, monkeypatch, tmp_path):
    _task(monkeypatch, tmp_path / "ctx", "probe", PROBE)
    monkeypatch.setenv("PROBE_EXIT", "2")
    assert store_tasks.run(repo_store, "probe", [])["exit"] == 2


def test_a_task_past_its_timeout_is_stopped(repo_store, monkeypatch, tmp_path):
    _task(monkeypatch, tmp_path / "ctx", "slow", "import time\ntime.sleep(30)\n", timeout=1)
    out = store_tasks.run(repo_store, "slow", [])
    assert out["exit"] == 124 and "stopped after 1s" in out["stderr"]


def test_a_task_write_is_a_daemon_write_and_gets_committed(repo_store, monkeypatch, tmp_path):
    root = tmp_path / "ctx"
    _task(monkeypatch, root, "probe", PROBE)
    out = store_tasks.run(repo_store, "probe", ["--write"])
    assert out["changed"] == ["global/memory/t.md"]
    assert "global/memory/t.md" in repo_store.ledger.snapshot()
    proc = repo_store._commit_dirty("task write")
    assert proc is not None and proc.returncode == 0, proc and proc.stderr
    assert _git(root, "show", "HEAD:global/memory/t.md").stdout == "from a task\n"
    assert "global/memory/t.md" not in repo_store.ledger.snapshot()


def test_a_readonly_subcommand_skips_the_dirty_scan(repo_store, monkeypatch, tmp_path):
    _task(monkeypatch, tmp_path / "ctx", "probe", PROBE)
    monkeypatch.setitem(store_tasks.TASKS, "probe",
                        store_tasks.Task(30, readonly=frozenset({"status"})))
    scans = []
    real = store_tasks._dirty
    monkeypatch.setattr(store_tasks, "_dirty", lambda root: scans.append(root) or real(root))
    out = store_tasks.run(repo_store, "probe", ["status"])
    assert out["exit"] == 0 and out["changed"] == [] and scans == []
    store_tasks.run(repo_store, "probe", ["lock"])
    assert len(scans) == 2


def test_test_lock_reads_are_readonly():
    assert store_tasks.TASKS["test-lock"].readonly == {"status", "check"}




WARM_PROBE = """import os, sys, time
if "--leak" in sys.argv:
    os.environ["LEAKED"] = "yes"
if "--sleep" in sys.argv:
    time.sleep(30)
print("ppid=%d" % os.getppid())
print("server-task=" + os.environ.get("AGENT_CONTEXT_SERVER_TASK", ""))
print("leaked=" + os.environ.get("LEAKED", ""))
print("cwd=" + os.getcwd())
print("args=" + " ".join(sys.argv[1:]))
print("stdin=" + sys.stdin.read())
sys.exit(int(os.environ.get("PROBE_EXIT", "0")))
"""


@pytest.fixture
def warm(monkeypatch, tmp_path):
    runner = store_tasks.WarmRunner()
    monkeypatch.setattr(store_tasks, "_WARM", runner)
    _task(monkeypatch, tmp_path / "ctx", "probe", WARM_PROBE)
    monkeypatch.setitem(store_tasks.TASKS, "probe",
                        store_tasks.Task(2, readonly=frozenset({"status"})))
    yield runner
    runner._kill()


def _field(out, name):
    return next(line.split("=", 1)[1] for line in out["stdout"].splitlines()
                if line.startswith(name + "="))


def test_a_readonly_call_runs_in_the_warm_worker(repo_store, warm, tmp_path):
    out = store_tasks.run(repo_store, "probe", ["status", "x y"], stdin="hello")
    assert out["exit"] == 0, out
    assert warm._proc is not None
    assert int(_field(out, "ppid")) == warm._proc.pid        
    assert _field(out, "server-task") == "1"
    assert _field(out, "args") == "status x y"
    assert _field(out, "stdin") == "hello"
    assert _field(out, "cwd") == str(tmp_path / "ctx")
    first = warm._proc.pid
    store_tasks.run(repo_store, "probe", ["status"])
    assert warm._proc.pid == first                            


def test_a_writing_call_still_runs_cold(repo_store, warm):
    out = store_tasks.run(repo_store, "probe", ["lock"])
    assert int(_field(out, "ppid")) == os.getpid() and warm._proc is None


def test_warm_calls_are_isolated_and_relay_the_exit_code(repo_store, warm, monkeypatch):
    store_tasks.run(repo_store, "probe", ["status", "--leak"])
    assert _field(store_tasks.run(repo_store, "probe", ["status"]), "leaked") == ""
    monkeypatch.setenv("PROBE_EXIT", "2")
    assert store_tasks.run(repo_store, "probe", ["status"])["exit"] == 2


def test_a_warm_call_past_its_timeout_is_stopped_and_the_next_one_works(repo_store, warm):
    out = store_tasks.run(repo_store, "probe", ["status", "--sleep"])
    assert out["exit"] == 124 and "stopped after 2s" in out["stderr"]
    assert warm._proc is None
    assert store_tasks.run(repo_store, "probe", ["status"])["exit"] == 0


def test_an_edited_script_runs_its_new_text_warm(repo_store, warm, tmp_path):
    store_tasks.run(repo_store, "probe", ["status"])
    script = tmp_path / "ctx" / "global" / "scripts" / "probe.py"
    script.write_text("print('edited')\n")
    out = store_tasks.run(repo_store, "probe", ["status"])
    assert out["stdout"] == "edited\n" and warm._proc is not None


def test_a_dead_worker_is_replaced(repo_store, warm):
    store_tasks.run(repo_store, "probe", ["status"])
    warm._proc.kill()
    warm._proc.wait()
    out = store_tasks.run(repo_store, "probe", ["status"])
    assert out["exit"] == 0 and int(_field(out, "ppid")) == warm._proc.pid


def test_the_tool_runs_a_task_off_the_event_loop(repo_store, monkeypatch, tmp_path):
    _task(monkeypatch, tmp_path / "ctx", "probe", PROBE)
    monkeypatch.setattr(server, "_get_conn", lambda: repo_store)
    out = json.loads(asyncio.run(server.run_store_task("probe", ["a"])))
    assert out["exit"] == 0 and "args=a" in out["stdout"]


@pytest.mark.parametrize("stdin", ['{"entry": {"transport": "http"}}', {"entry": {"transport": "http"}}, ["entry"]])
def test_tool_accepts_json_stdin_after_mcp_coercion(repo_store, monkeypatch, tmp_path, stdin):
    _task(monkeypatch, tmp_path / "ctx", "probe", PROBE)
    monkeypatch.setattr(server, "_get_conn", lambda: repo_store)
    tool = server.mcp._tool_manager.get_tool("run_store_task")
    out = json.loads(asyncio.run(tool.run({"task": "probe", "stdin": stdin})))
    assert out["exit"] == 0
    expected = json.loads(stdin) if isinstance(stdin, str) else stdin
    assert "stdin=" + json.dumps(expected) in out["stdout"]




def test_write_atomic_under_the_root_is_recorded_and_elsewhere_is_not(repo_store, tmp_path):
    root = tmp_path / "ctx"
    paths.write_atomic(root / "global" / "memory" / "a.md", "a\n")
    paths.write_atomic(tmp_path / "outside.txt", "x\n")
    assert repo_store.ledger.snapshot() == {"global/memory/a.md"}


def test_an_outside_edit_is_reported_and_never_committed(repo_store, tmp_path):
    root = tmp_path / "ctx"
    paths.write_atomic(root / "global" / "memory" / "ours.md", "ours\n")
    (root / "global" / "memory" / "obsidian.md").write_text("typed in Obsidian\n")
    proc = repo_store._commit_dirty("mixed")
    assert proc is not None and proc.returncode == 0
    tracked = _git(root, "ls-files").stdout.split()
    assert "global/memory/ours.md" in tracked
    assert "global/memory/obsidian.md" not in tracked
    assert repo_store.outside_edits == ["global/memory/obsidian.md"]


def test_an_outside_edit_alone_commits_nothing(repo_store, tmp_path):
    root = tmp_path / "ctx"
    head = _git(root, "rev-parse", "HEAD").stdout
    (root / "global" / ".keep").write_text("edited by hand\n")
    assert repo_store._commit_dirty("nothing of ours") is None
    assert _git(root, "rev-parse", "HEAD").stdout == head
    assert repo_store.outside_edits == ["global/.keep"]


def test_a_recorded_deletion_is_committed(repo_store, tmp_path):
    root = tmp_path / "ctx"
    (root / "global" / ".keep").unlink()
    repo_store._arm_commit(str(root / "global" / ".keep"))
    proc = repo_store._commit_dirty("delete")
    assert proc is not None and proc.returncode == 0
    assert "global/.keep" not in _git(root, "ls-files").stdout.split()


def test_a_ledger_entry_that_is_no_longer_dirty_is_dropped(repo_store, tmp_path):
    root = tmp_path / "ctx"
    repo_store.ledger.add(["global/.keep"])        
    assert repo_store._commit_dirty("noop") is None
    assert repo_store.ledger.snapshot() == set()
    assert (root / "global" / ".keep").exists()


def test_the_ledger_survives_a_restart(tmp_path):
    state = tmp_path / "ledger.json"
    first = write_ledger.WriteLedger(str(tmp_path / "r"), str(state))
    first.add(["global/memory/a.md"])
    assert write_ledger.WriteLedger(str(tmp_path / "r"), str(state)).snapshot() == {
        "global/memory/a.md"}
