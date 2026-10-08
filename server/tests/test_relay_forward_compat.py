'A relay keeps working when the bundle gains a key type it does not know, and it updates itself.'
import json
import logging
from pathlib import Path

import pytest

from agent_context import relay_materialize as R
from agent_context import relay_update as U
from agent_context import server as S

TOKEN = "s3cret-token-for-forward-compat"

SETTINGS_SCRIPT = (
    "import json, os, pathlib\n"
    "home = pathlib.Path(os.environ['HOME'])\n"
    "out = home / '.claude' / 'settings.json'\n"
    "out.parent.mkdir(parents=True, exist_ok=True)\n"
    "out.write_text(json.dumps({'home': str(home)}))\n"
)

KNOWN: dict[str, str] = {
    "commands/guide.md": "guide\n",
    "hooks/guard.py": "#!/usr/bin/env python3\nprint('guard')\n",
    "scripts/home-settings-sync.py": SETTINGS_SCRIPT,
}
FUTURE = "futurekind/thing.json"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AGENT_CONTEXT_HOST", "AGENT_CONTEXT_PORT", "AGENT_CONTEXT_TOKEN",
                 "AGENT_CONTEXT_TRANSPORT", "AGENT_CONTEXT_NO_DAEMON", "AGENT_CONTEXT_STORE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def home(exec_capable_tmp_path: Path) -> Path:
    
    
    h = exec_capable_tmp_path / "home"
    h.mkdir()
    return h


def _serve(monkeypatch: pytest.MonkeyPatch, bundle: dict[str, str]) -> None:
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", TOKEN)
    monkeypatch.setattr(R, "fetch_bundle_mcp", lambda url, token, timeout: dict(bundle))




def test_apply_skips_an_unknown_prefix_and_writes_every_known_key(
        home: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="agent-context")
    result = R.apply_bundle({**KNOWN, FUTURE: "{}"}, home)
    for key, content in KNOWN.items():
        assert R.target_for(key, home).read_text() == content
    assert sorted(result.written) == sorted(KNOWN)
    assert result.skipped == [FUTURE]
    assert not (home / "futurekind").exists()
    assert not any(FUTURE.split("/")[0] in str(p) for p in home.rglob("*"))
    warnings = [r for r in caplog.records if FUTURE in r.getMessage()]
    assert len(warnings) == 1


def test_several_unknown_keys_make_one_warning_that_names_them_all(
        home: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="agent-context")
    keys = ["futurekind/a.json", "futurekind/b.json", "otherkind/c.md"]
    result = R.apply_bundle({**KNOWN, **{k: "x" for k in keys}}, home)
    assert result.skipped == sorted(keys)
    named = [r for r in caplog.records if all(k in r.getMessage() for k in keys)]
    assert len(named) == 1


def test_a_bundle_with_only_known_keys_skips_nothing(home: Path) -> None:
    assert R.apply_bundle(KNOWN, home).skipped == []


def test_a_start_with_an_unknown_key_is_applied_not_degraded(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, {**KNOWN, FUTURE: "{}"})
    assert R.materialize_on_start(home) is True
    assert (home / ".claude" / "settings.json").is_file()  
    assert R.target_for("commands/guide.md", home).read_text() == "guide\n"


def test_refresh_reports_the_skipped_keys(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, {**KNOWN, FUTURE: "{}"})
    result = R.refresh(home)
    assert result is not None and result.skipped == [FUTURE]


def test_target_for_still_refuses_an_unknown_prefix(home: Path) -> None:
    with pytest.raises(R.BundleError):
        R.target_for(FUTURE, home)




@pytest.mark.parametrize("bad", [
    "/etc/passwd", "../escape.md", "docs/../../escape.md", "docs\\evil.md", "docs/a\0b.md",
    "docs/", "docs", "futurekind/../../escape.md", "futurekind/",
])
def test_an_unsafe_key_refuses_the_whole_bundle_before_any_write(home: Path, bad: str) -> None:
    with pytest.raises(R.BundleError):
        R.apply_bundle({**KNOWN, FUTURE: "{}", bad: "bad"}, home)
    assert not R.target_for("commands/guide.md", home).exists()
    assert not (home.parent / "escape.md").exists()


def test_an_unsafe_key_fails_a_start_and_leaves_the_cache_alone(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, {**KNOWN, "../escape.md": "bad"})
    assert R.materialize_on_start(home) is False
    assert R.read_cache(home) is None




def test_the_cache_leaves_out_a_skipped_key(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, {**KNOWN, FUTURE: "{}"})
    assert R.refresh(home) is not None
    assert R.read_cache(home) == KNOWN


def test_a_newer_relay_applies_what_an_older_one_skipped(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = {**KNOWN, FUTURE: '{"v": 1}'}
    _serve(monkeypatch, bundle)
    assert R.refresh(home) is not None
    monkeypatch.setitem(R._ROOTS, "futurekind", (".agent-context", "future"))
    result = R.refresh(home)
    assert result is not None and result.skipped == []
    assert (home / ".agent-context" / "future" / "thing.json").read_text() == '{"v": 1}'
    assert R.read_cache(home) == bundle


def test_prune_never_touches_a_file_a_skipped_key_might_name(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mine = home / "futurekind" / "thing.json"
    mine.parent.mkdir()
    mine.write_text("mine\n")
    _serve(monkeypatch, {**KNOWN, FUTURE: "{}"})
    assert R.refresh(home) is not None
    _serve(monkeypatch, KNOWN)
    result = R.refresh(home)
    assert result is not None and result.pruned == []
    assert mine.read_text() == "mine\n"




def test_the_cached_bundle_path_skips_an_unknown_key(home: Path) -> None:
    cache = R.cache_path(home)
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({**KNOWN, FUTURE: "{}"}))
    assert R.apply_cached(home) is True
    assert R.target_for("hooks/guard.py", home).is_file()
    assert (home / ".claude" / "settings.json").is_file()
    assert not (home / "futurekind").exists()


def test_the_content_changed_refresh_skips_an_unknown_key(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, {**KNOWN, FUTURE: "{}"})
    R._refresh_and_sync(home, 5.0)
    assert R.target_for("commands/guide.md", home).is_file()
    assert (home / ".claude" / "settings.json").is_file()
    assert R.read_cache(home) == KNOWN



