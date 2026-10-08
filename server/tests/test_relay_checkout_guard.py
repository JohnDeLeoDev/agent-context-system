"A relay-only host must never materialize its bundle over a git checkout.\n\nIncident: pc's ~/.agent-context was itself a fleet checkout (a bootstrap cloned it) while\nAGENT_CONTEXT_HOST pointed at ls, so is_remote() was true and relay_materialize overwrote\nthe clone with its own projection -- hooks/scripts flipped to 0755, global/commands/*.md and\nglobal/skills/*/SKILL.md lost their frontmatter -- and pc's autosync then committed the\ndamage (store commit b28db2f08, repaired). The guard lives in apply_bundle, the one place\nevery write path (refresh's startup call, ContentRefresher's cycle, and apply_cached's\nls-down fallback) funnels through, so all three inherit it."
from __future__ import annotations

import json
import logging
import stat
from pathlib import Path

import pytest

from agent_context import relay_materialize as R

BUNDLE = {
    "commands/guide.md": "guide\n",
    "hooks/guard.py": "#!/usr/bin/env python3\nprint('guard')\n",
}


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def _make_checkout(home: Path) -> None:
    (home / ".agent-context" / ".git").mkdir(parents=True)


def _preexisting_file(home: Path, key: str, content: str, mode: int) -> Path:
    path = R.target_for(key, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)
    return path




def test_is_git_checkout_true_for_a_dot_git_directory(home: Path) -> None:
    _make_checkout(home)
    assert R.is_git_checkout(home) is True


def test_is_git_checkout_true_for_a_dot_git_file(home: Path) -> None:
    
    (home / ".agent-context").mkdir()
    (home / ".agent-context" / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
    assert R.is_git_checkout(home) is True


def test_is_git_checkout_false_with_no_git_marker(home: Path) -> None:
    (home / ".agent-context").mkdir()
    assert R.is_git_checkout(home) is False


def test_is_git_checkout_false_with_no_agent_context_dir_at_all(home: Path) -> None:
    assert R.is_git_checkout(home) is False




def test_apply_bundle_writes_nothing_for_a_checkout_root(home: Path) -> None:
    _make_checkout(home)
    original = "cmd: existing\n"
    path = _preexisting_file(home, "commands/guide.md", original, 0o644)
    before_mtime = path.stat().st_mtime_ns
    result = R.apply_bundle(BUNDLE, home)
    assert result.blocked is True
    assert result.written == [] and result.unchanged == [] and result.pruned == []
    assert path.read_text() == original
    assert path.stat().st_mode & 0o777 == 0o644
    assert path.stat().st_mtime_ns == before_mtime


def test_apply_bundle_touches_no_new_file_for_a_checkout_root(home: Path) -> None:
    _make_checkout(home)
    R.apply_bundle(BUNDLE, home)
    assert not R.target_for("commands/guide.md", home).exists()
    assert not R.target_for("hooks/guard.py", home).exists()


def test_apply_bundle_leaves_mode_alone_for_a_checkout_root(home: Path) -> None:
    "The incident's other symptom: hooks/scripts flipped to 0755. A checkout-guarded apply\n    must not touch mode even when the bundle's own file is unchanged in content."
    _make_checkout(home)
    path = _preexisting_file(home, "hooks/guard.py", BUNDLE["hooks/guard.py"], 0o644)
    R.apply_bundle(BUNDLE, home)
    assert path.stat().st_mode & 0o777 == 0o644


def test_apply_bundle_warns_once_per_process_for_the_same_root(
        home: Path, caplog: pytest.LogCaptureFixture) -> None:
    _make_checkout(home)
    with caplog.at_level(logging.WARNING, logger="agent-context"):
        R.apply_bundle(BUNDLE, home)
        R.apply_bundle(BUNDLE, home)
    hits = [r for r in caplog.records
           if r.name == "agent-context" and "is a git checkout" in r.getMessage()]
    assert len(hits) == 1
    assert str(home) in hits[0].getMessage()


def test_apply_bundle_still_applies_for_a_non_checkout_root(home: Path) -> None:
    'Non-regression: a plain relay-only home is unaffected by the guard.'
    result = R.apply_bundle(BUNDLE, home)
    assert result.blocked is False
    assert sorted(result.written) == sorted(BUNDLE)
    assert R.target_for("commands/guide.md", home).read_text() == "guide\n"




def test_refresh_writes_nothing_and_caches_nothing_for_a_checkout_root(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_checkout(home)
    original = "cmd: existing\n"
    path = _preexisting_file(home, "commands/guide.md", original, 0o644)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "t")
    monkeypatch.setattr(R, "fetch_bundle_mcp", lambda *a, **k: dict(BUNDLE))
    result = R.refresh(home, timeout=1.0)
    assert result is not None and result.blocked is True
    assert path.read_text() == original
    assert R.read_cache(home) is None


def test_refresh_still_writes_and_caches_for_a_non_checkout_root(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    "Non-regression: refresh()'s own cache write is unaffected by the guard."
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "t")
    monkeypatch.setattr(R, "fetch_bundle_mcp", lambda *a, **k: dict(BUNDLE))
    result = R.refresh(home, timeout=1.0)
    assert result is not None and result.blocked is False
    assert R.target_for("commands/guide.md", home).read_text() == "guide\n"
    assert R.read_cache(home) == BUNDLE




def test_materialize_on_start_runs_no_sync_for_a_checkout_root(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    'test materialize on start runs no sync for a checkout root.'
    _make_checkout(home)
    original = "cmd: existing\n"
    path = _preexisting_file(home, "commands/guide.md", original, 0o644)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "t")
    monkeypatch.setattr(R, "fetch_bundle_mcp", lambda *a, **k: dict(BUNDLE))
    ran: list[str] = []
    monkeypatch.setattr(R, "run_settings_sync", lambda *a, **k: ran.append("settings") or True)
    monkeypatch.setattr(R, "run_home_materialize", lambda *a, **k: ran.append("materialize"))
    assert R.materialize_on_start(home, timeout=1.0) is True
    assert path.read_text() == original
    assert ran == []


def test_materialize_on_start_still_syncs_for_a_non_checkout_root(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    'Non-regression: a relay-only host still applies, syncs settings and materializes.'
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "t")
    monkeypatch.setattr(R, "fetch_bundle_mcp", lambda *a, **k: dict(BUNDLE))
    ran: list[str] = []
    monkeypatch.setattr(R, "run_settings_sync", lambda *a, **k: ran.append("settings") or True)
    monkeypatch.setattr(R, "run_home_materialize", lambda *a, **k: ran.append("materialize"))
    assert R.materialize_on_start(home, timeout=1.0) is True
    assert ran == ["settings", "materialize"]




def test_apply_cached_writes_nothing_for_a_checkout_root(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_checkout(home)
    original = "cmd: existing\n"
    path = _preexisting_file(home, "commands/guide.md", original, 0o644)
    R.write_cache(BUNDLE, home)
    ran: list[str] = []
    monkeypatch.setattr(R, "run_settings_sync", lambda *a, **k: ran.append("settings") or True)
    monkeypatch.setattr(R, "run_home_materialize", lambda *a, **k: ran.append("materialize"))
    assert R.apply_cached(home) is False
    assert path.read_text() == original
    assert ran == []


def test_apply_cached_still_applies_for_a_non_checkout_root(home: Path) -> None:
    'Non-regression: the ls-down cache fallback is unaffected by the guard.'
    R.write_cache(BUNDLE, home)
    assert R.apply_cached(home) is True
    assert R.target_for("commands/guide.md", home).read_text() == "guide\n"
    assert R.target_for("hooks/guard.py", home).stat().st_mode & stat.S_IXUSR


def test_write_cache_json_round_trips_for_the_non_regression_fixture() -> None:
    
    assert json.loads(json.dumps(BUNDLE)) == BUNDLE
