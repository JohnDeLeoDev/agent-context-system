'test precommit gate install.'
import subprocess

import pytest
from fixture_signing import signing_config

from agent_context.server import _PRECOMMIT_GATE_MARKER, ensure_precommit_gate


def _dir_allows_exec(directory):
    ' dir allows exec.'
    probe = directory / "exec-probe"
    probe.write_text("#!/bin/sh\nexit 0\n")
    probe.chmod(0o755)
    try:
        return subprocess.run([str(probe)], timeout=10).returncode == 0
    except OSError:
        return False


def _store(tmp_path):
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / "global" / "scripts").mkdir(parents=True)
    script = tmp_path / "global" / "scripts" / "store-precommit-gate.py"
    script.write_text("import sys\nsys.exit(0)\n")
    return tmp_path, script


def _hook(root):
    return root / ".git" / "hooks" / "pre-commit"


def test_installs_a_launcher_when_absent(tmp_path):
    root, script = _store(tmp_path)
    hook = _hook(root)
    assert not hook.exists()

    assert ensure_precommit_gate(root) is not None
    assert hook.is_file() and not hook.is_symlink()
    content = hook.read_text()
    assert _PRECOMMIT_GATE_MARKER in content
    assert str(script) in content
    assert (hook.stat().st_mode & 0o777) == 0o755


def test_a_dangling_symlink_to_the_old_shell_script_is_replaced(tmp_path):
    'The old wiring: a symlink to store-precommit-gate.sh, which no longer exists.'
    root, _script = _store(tmp_path)
    hook = _hook(root)
    hook.symlink_to(root / "global" / "scripts" / "store-precommit-gate.sh")
    assert hook.is_symlink() and not hook.exists()          

    assert ensure_precommit_gate(root) is not None
    assert hook.is_file() and not hook.is_symlink()
    assert _PRECOMMIT_GATE_MARKER in hook.read_text()


def test_a_live_symlink_is_replaced(tmp_path):
    'test a live symlink is replaced.'
    root, script = _store(tmp_path)
    hook = _hook(root)
    hook.symlink_to(script)
    assert hook.is_symlink() and hook.exists()

    assert ensure_precommit_gate(root) is not None
    assert hook.is_file() and not hook.is_symlink()
    assert _PRECOMMIT_GATE_MARKER in hook.read_text()


def test_a_stale_generated_launcher_is_rewritten(tmp_path):
    root, script = _store(tmp_path)
    hook = _hook(root)
    hook.write_text(f"#!/bin/sh\n{_PRECOMMIT_GATE_MARKER}\n"
                    'exec /old/python3 /old/store-precommit-gate.py "$@"\n')
    hook.chmod(0o755)

    assert ensure_precommit_gate(root) is not None
    content = hook.read_text()
    assert _PRECOMMIT_GATE_MARKER in content
    assert str(script) in content
    assert "/old/store-precommit-gate.py" not in content


def test_a_hand_written_hook_without_the_marker_is_left_untouched(tmp_path):
    'A machine-local customization wins — this guard installs, it does not police.'
    root, _script = _store(tmp_path)
    hook = _hook(root)
    hook.write_text("#!/bin/sh\n# hand-written\nexit 0\n")

    assert ensure_precommit_gate(root) is None
    assert "hand-written" in hook.read_text()
    assert not hook.is_symlink()


def test_a_second_call_changes_nothing(tmp_path):
    root, _script = _store(tmp_path)
    hook = _hook(root)
    assert ensure_precommit_gate(root) is not None
    before_content = hook.read_text()
    before_mtime = hook.stat().st_mtime_ns

    assert ensure_precommit_gate(root) is None
    assert hook.read_text() == before_content
    assert hook.stat().st_mtime_ns == before_mtime


def test_silent_no_op_when_this_is_not_a_store(tmp_path):
    'Never raise: a store served from a tarball, a test fixture, or a linked\n    worktree (where .git is a FILE) must not fail startup over a missing guard.'
    assert ensure_precommit_gate(tmp_path) is None          

    root, _ = _store(tmp_path / "s")
    (root / ".git").rename(root / ".git-moved")
    (root / ".git").write_text("gitdir: /elsewhere\n")      
    assert ensure_precommit_gate(root) is None


def _hermetic_env(home):
    ' hermetic env.'
    import os
    keep = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR")
            if k in os.environ}
    return {
        **keep,
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }


def test_git_actually_runs_the_generated_launcher(tmp_path):
    'End to end, proving the launcher is not just correct text: a real git commit\n    with a gate script that exits 1 must be refused.'
    if not _dir_allows_exec(tmp_path):
        pytest.skip("tmp_path is on a noexec filesystem (Synology /tmp): git cannot run "
                    "any hook there, so a refused commit cannot be observed")
    root = tmp_path / "store"
    (root / "global" / "scripts").mkdir(parents=True)
    (root / "global" / "scripts" / "store-precommit-gate.py").write_text(
        "import sys\nsys.exit(1)\n")
    env = _hermetic_env(tmp_path / "home")
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)],
                   check=True, env=env, timeout=30)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"),
                 *signing_config(tmp_path)):
        subprocess.run(["git", "-C", str(root), "config", k, v], check=True,
                       env=env, timeout=30)

    assert ensure_precommit_gate(root) is not None

    (root / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=env,
                   timeout=30)
    result = subprocess.run(["git", "-C", str(root), "commit", "-m", "test"],
                            capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode != 0
