"- a Windows relay installs a new release for the next session and never freezes its stdin or\n    execs, since Windows has no exec that keeps the process;\n  - a release's relay command and the installer's command have Windows forms;\n  - every git read runs with stdin on the null device, or its child hangs on Windows;\n  - the flock module keeps POSIX semantics where fcntl exists."
import os
import subprocess
from pathlib import Path

import anyio
import httpx
import pytest

from agent_context import flock, project_resolve, relay_stdio, relay_swap
from agent_context.relay_source import RELEASE_HEADER


def _response(etag: str) -> httpx.Response:
    return httpx.Response(200, headers={RELEASE_HEADER: etag},
                          request=httpx.Request("POST", "http://x/mcp"))


def test_a_windows_relay_installs_the_new_release_and_keeps_serving(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    r, w = os.pipe()
    pipe = relay_stdio.StdioPipe(fd_in=r)
    monkeypatch.setattr(relay_stdio, "ACTIVE", pipe)
    monkeypatch.setattr(relay_swap, "SWAPS_IN_PLACE", False)
    installs: list[str] = []
    monkeypatch.setattr(relay_swap, "install", lambda etag: installs.append(etag) or tmp_path)
    execs: list[object] = []
    monkeypatch.setattr(relay_swap, "exec_release", lambda t, s: execs.append(t))
    st = {"init_msg": None, "init_note": None, "init_answered": False, "pending": {},
          "stdin_handed_over": False, "client_eof": False}

    async def main() -> set[str]:
        watch = relay_swap.ReleaseWatch('"old"')
        async with anyio.create_task_group() as tg:
            tg.start_soon(relay_swap.swap_when_released, watch, st)
            await watch.on_response(_response('"new"'))
            await anyio.sleep(0.3)
            await watch.on_response(_response('"new"'))  
            await anyio.sleep(0.2)
            tg.cancel_scope.cancel()
        return watch.failed

    assert anyio.run(main) == {'"new"'}
    assert installs == ['"new"'] and execs == [] and not pipe.frozen
    os.close(r)
    os.close(w)


def test_a_release_command_is_bin_on_posix_and_scripts_exe_on_windows(
        monkeypatch: pytest.MonkeyPatch) -> None:
    assert relay_swap.release_program(Path("r")) == Path("r") / "bin" / "agent-context"
    monkeypatch.setattr(relay_swap, "WINDOWS", True)
    assert relay_swap.release_program(Path("r")) == Path("r") / "Scripts" / "agent-context.exe"


def test_windows_runs_the_installer_under_this_python(monkeypatch: pytest.MonkeyPatch) -> None:
    installer = Path("home") / ".local" / "bin" / "agent-context-relay-install"
    assert relay_swap.installer_command(installer, '"e"') == [str(installer), "--release", '"e"']
    monkeypatch.setattr(relay_swap, "WINDOWS", True)
    assert relay_swap.installer_command(installer, '"e"')[:2] == [relay_swap.sys.executable,
                                                                    str(installer)]


def test_every_git_read_runs_with_stdin_on_the_null_device(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[dict] = []

    def run(argv, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, "ssh://h/r\n", "")

    monkeypatch.setattr(project_resolve.subprocess, "run", run)
    project_resolve.get_git_remote(tmp_path)
    project_resolve.get_git_remotes(tmp_path)
    project_resolve.get_repo_root(tmp_path)
    project_resolve.get_git_branch(tmp_path)
    assert seen and all(k.get("stdin") is subprocess.DEVNULL for k in seen)


def test_flock_is_fcntls_own_where_it_exists(tmp_path: Path) -> None:
    import fcntl
    assert flock.flock is fcntl.flock
    path = tmp_path / "lock"
    with open(path, "w") as first, open(path, "w") as second:
        flock.flock(first, flock.LOCK_EX | flock.LOCK_NB)
        with pytest.raises(BlockingIOError):
            flock.flock(second, flock.LOCK_EX | flock.LOCK_NB)
        flock.flock(first, flock.LOCK_UN)
        flock.flock(second, flock.LOCK_EX | flock.LOCK_NB)


def test_native_windows_names_itself_by_its_machine_guid(monkeypatch) -> None:
    "Windows has no /etc/machine-id; its install GUID is the stable id, and it differs\n    from the WSL distribution's, so native Windows and WSL are two machine rows."
    import sys
    import types

    from agent_context import machine

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake = types.SimpleNamespace(
        HKEY_LOCAL_MACHINE=object(),
        OpenKey=lambda root, path: Key(),
        QueryValueEx=lambda key, name: ("15800eaa-c12d-446e-b77f-3a1ee3c0a235", 1))
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(machine.platform, "system", lambda: "Windows")
    assert machine._os_uuid() == "15800eaa-c12d-446e-b77f-3a1ee3c0a235"
