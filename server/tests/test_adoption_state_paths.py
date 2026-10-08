'adoption.py and machine.py name paths and ids that must exist on every host.\n\n* adoption.STAMP read ~/.claude/state/health/materialize-stamp.json, but the stamp is written\n  under ~/.local/state/agent-context/health, so every machine reported projection `at: None`.\n* adoption.LSPD named ~/.claude/scripts/lspd.py, a directory retire removes, so every machine\n  reported its stale-daemon list as unknown.\n* machine.get_machine_uuid() falls back to `host:<hostname>` when the OS gives no UUID (native\n  Windows). The colon cannot exist in a Windows path and the id becomes a directory name.'
import re
from pathlib import Path

from agent_context import adoption, machine

ILLEGAL_ON_WINDOWS = re.compile(r'[<>:"/\\|?*]')


def test_stamp_is_read_from_the_health_dir() -> None:
    
    assert Path.home() / ".local" / "state" / "agent-context" / "health" / "materialize-stamp.json" == adoption.STAMP
    assert ".claude" not in adoption.STAMP.parts


def test_lspd_is_run_from_the_store() -> None:
    assert Path.home() / ".agent-context" / "global" / "scripts" / "lspd.py" == adoption.LSPD
    assert ".claude" not in adoption.LSPD.parts


def _fresh(monkeypatch, os_uuid: str | None) -> str:
    monkeypatch.setattr(machine, "_CACHED_UUID", None)
    monkeypatch.delenv("AGENT_CONTEXT_MACHINE_ID", raising=False)
    monkeypatch.setattr(machine, "_os_uuid", lambda: os_uuid)
    return machine.get_machine_uuid()


def test_fallback_machine_id_is_a_legal_path_component(monkeypatch) -> None:
    monkeypatch.setattr(machine.socket, "gethostname", lambda: "users-PC")
    got = _fresh(monkeypatch, None)
    assert got == "host-users-PC"
    assert not ILLEGAL_ON_WINDOWS.search(got)


def test_a_real_os_uuid_is_returned_unchanged(monkeypatch) -> None:
    assert _fresh(monkeypatch, "B5B75545-B2E5-59A1-B327-47062408E8A2") == "B5B75545-B2E5-59A1-B327-47062408E8A2"


def test_the_env_override_still_wins(monkeypatch) -> None:
    monkeypatch.setattr(machine, "_CACHED_UUID", None)
    monkeypatch.setenv("AGENT_CONTEXT_MACHINE_ID", "forced-id")
    monkeypatch.setattr(machine, "_os_uuid", lambda: "from-os")
    assert machine.get_machine_uuid() == "forced-id"
