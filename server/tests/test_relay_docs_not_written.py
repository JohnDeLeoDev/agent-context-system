'The relay no longer writes bundle `docs/` keys under ~/.agent-context/shared-docs/.\n\nA machine with no store checkout reads docs over MCP, so the relay drops the `docs/`\nkeys: they are not written, and they are not kept in the cached bundle either (the cache is the\nls-down record, and docs are not applied from it). Files an older relay wrote under\nshared-docs/ are removed once, when their bytes still equal what the previous cache recorded;\na hand-edited file and a file the cache never listed stay. Everything else in the bundle is\nhandled as before.'
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_context import relay_materialize as R

TOKEN = "s3cret-token"

BUNDLE: dict[str, str] = {
    "hooks/guard.py": "#!/usr/bin/env python3\nprint('guard')\n",
    "scripts/tool.py": "#!/usr/bin/env python3\nprint('tool')\n",
    "scripts/home-settings-sync.py": "import sys\nsys.exit(0)\n",
    "scripts/home-materialize.py": "import sys\nsys.exit(0)\n",
    "skills/demo/SKILL.md": "# demo skill\n",
    "commands/go.md": "go\n",
    "docs/guide.md": "guide\n",
    "docs/nested/deep.md": "deep\n",
    "docs/Alpha/cmd.md": "project command doc\n",
}
DOC_KEYS = [k for k in BUNDLE if k.startswith("docs/")]
NON_DOC = {k: v for k, v in BUNDLE.items() if not k.startswith("docs/")}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AGENT_CONTEXT_HOST", "AGENT_CONTEXT_PORT", "AGENT_CONTEXT_TOKEN",
                 "AGENT_CONTEXT_TRANSPORT", "AGENT_CONTEXT_NO_DAEMON", "AGENT_CONTEXT_STORE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    bundle = dict(BUNDLE)
    monkeypatch.setattr(R, "fetch_bundle_mcp", lambda url, token, timeout: dict(bundle))
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", TOKEN)
    return bundle


@pytest.fixture
def no_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> dict[str, str]:
        raise AssertionError("the cached path must not fetch")
    monkeypatch.setattr(R, "fetch_bundle_mcp", refuse)


def _shared_docs(home: Path) -> Path:
    return home / ".agent-context" / "shared-docs"


def _cached_keys(home: Path) -> set[str]:
    return set(json.loads(R.cache_path(home).read_text()))


def _old_relay_state(home: Path) -> None:
    'What the relay left before this change: docs written under shared-docs and in the cache.'
    for key, text in BUNDLE.items():
        if key.startswith("docs/"):
            path = _shared_docs(home).joinpath(*key.split("/")[1:])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    R.write_cache(BUNDLE, home)




def test_apply_bundle_writes_no_docs_and_makes_no_shared_docs_dir(home: Path) -> None:
    result = R.apply_bundle(dict(BUNDLE), home)
    assert not _shared_docs(home).exists()
    assert not [k for k in (*result.written, *result.unchanged) if k.startswith("docs/")]
    assert (home / ".agent-context" / "global" / "hooks" / "guard.py").is_file()
    assert (home / ".agent-context" / "global" / "commands" / "go.md").read_text() == "go\n"


def test_refresh_writes_no_docs(home: Path, served: dict[str, str]) -> None:
    result = R.refresh(home, 5.0)
    assert result is not None
    assert not _shared_docs(home).exists()
    assert (home / ".agent-context" / "global" / "scripts" / "tool.py").is_file()


def test_materialize_on_start_writes_no_docs(home: Path, served: dict[str, str]) -> None:
    assert R.materialize_on_start(home, 5.0) is True
    assert not _shared_docs(home).exists()




def test_the_cached_bundle_holds_no_docs_keys(home: Path, served: dict[str, str]) -> None:
    assert R.refresh(home, 5.0) is not None
    keys = _cached_keys(home)
    assert not [k for k in keys if k.startswith("docs/")], sorted(keys)
    assert keys == set(NON_DOC), sorted(keys ^ set(NON_DOC))


def test_an_ls_down_start_applies_a_cache_without_docs(home: Path, no_fetch: None) -> None:
    R.write_cache(NON_DOC, home)
    assert R.apply_cached(home) is True
    assert (home / ".agent-context" / "global" / "hooks" / "guard.py").is_file()
    assert not _shared_docs(home).exists()


def test_apply_cached_leaves_the_cache_file_byte_identical(home: Path, no_fetch: None) -> None:
    R.write_cache(NON_DOC, home)
    before = R.cache_path(home).read_bytes()
    assert R.apply_cached(home) is True
    assert R.cache_path(home).read_bytes() == before




def test_refresh_retires_the_docs_an_older_relay_wrote(home: Path, served: dict[str, str]) -> None:
    _old_relay_state(home)
    assert (_shared_docs(home) / "guide.md").is_file()
    assert R.refresh(home, 5.0) is not None
    assert not _shared_docs(home).exists(), sorted(p.name for p in _shared_docs(home).rglob("*"))
    assert not [k for k in _cached_keys(home) if k.startswith("docs/")]


def test_a_hand_edited_doc_stays_and_so_does_a_file_the_cache_never_listed(
        home: Path, served: dict[str, str]) -> None:
    _old_relay_state(home)
    (_shared_docs(home) / "guide.md").write_text("edited by hand\n")
    (_shared_docs(home) / "mine.md").write_text("not from the bundle\n")
    assert R.refresh(home, 5.0) is not None
    assert (_shared_docs(home) / "guide.md").read_text() == "edited by hand\n"
    assert (_shared_docs(home) / "mine.md").read_text() == "not from the bundle\n"
    assert not (_shared_docs(home) / "nested").exists()
    assert not (_shared_docs(home) / "Alpha").exists()


def test_the_cached_path_retires_old_docs_too(home: Path, no_fetch: None) -> None:
    _old_relay_state(home)
    before = R.cache_path(home).read_bytes()
    assert R.apply_cached(home) is True
    assert not _shared_docs(home).exists()
    assert R.cache_path(home).read_bytes() == before


def test_a_second_refresh_after_retirement_changes_nothing(
        home: Path, served: dict[str, str]) -> None:
    _old_relay_state(home)
    assert R.refresh(home, 5.0) is not None
    result = R.refresh(home, 5.0)
    assert result is not None
    assert result.written == [] and result.pruned == []
    assert not _shared_docs(home).exists()


def test_a_symlinked_shared_docs_dir_is_not_followed_out_of_the_home(
        home: Path, served: dict[str, str], tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "guide.md").write_text("guide\n")
    (home / ".agent-context").mkdir()
    (home / ".agent-context" / "shared-docs").symlink_to(outside)
    R.write_cache(BUNDLE, home)
    assert R.refresh(home, 5.0) is not None
    assert (outside / "guide.md").read_text() == "guide\n"




def test_a_global_docs_push_no_longer_affects_the_bundle() -> None:
    assert R.affects_bundle(["global/docs/guide.md"]) is False


def test_project_commands_push_affects_the_bundle_and_project_docs_do_not() -> None:
    assert R.affects_bundle(["projects/Alpha/docs/x.md"]) is False
    assert R.affects_bundle(["projects/Alpha/commands/x.md"]) is True
    assert R.affects_bundle(["global/commands/x.md"]) is True


def test_a_docs_key_does_not_call_for_a_home_materialize_run() -> None:
    assert R.needs_home_materialize(R.ApplyResult(written=["docs/guide.md"])) is False
    assert R.needs_home_materialize(R.ApplyResult(written=["skills/demo/SKILL.md"])) is True




def test_an_unsafe_docs_key_still_refuses_the_whole_bundle(home: Path) -> None:
    with pytest.raises(R.BundleError):
        R.apply_bundle({**NON_DOC, "docs/../escape.md": "x\n"}, home)
    assert not (home / ".agent-context" / "global" / "hooks" / "guard.py").exists()


def test_other_keys_are_still_pruned_when_the_daemon_drops_them(
        home: Path, served: dict[str, str]) -> None:
    assert R.refresh(home, 5.0) is not None
    served.pop("commands/go.md")
    result = R.refresh(home, 5.0)
    assert result is not None
    assert result.pruned == ["commands/go.md"]
    assert not (home / ".agent-context" / "global" / "commands" / "go.md").exists()
