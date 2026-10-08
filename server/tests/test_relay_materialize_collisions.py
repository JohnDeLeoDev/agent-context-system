"C6 review findings: the relay's writes must survive what is already on disk.\n\nTwo ways the local tree can disagree with the bundle, both found by reproducing them:\na directory (or a plain file) sitting where a bundle entry must go, which used to leave a\nhalf-written bundle, and a case-only rename on a case-insensitive volume, where the prune\nstep deleted the file it had just written because the old key names the same file."
import json
import logging
import tempfile
from pathlib import Path

import pytest

from agent_context import relay_materialize as R


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def _case_insensitive(directory: Path) -> bool:
    with tempfile.NamedTemporaryFile(dir=directory, prefix="probe-") as probe:
        return Path(probe.name.replace("probe-", "PROBE-")).exists()


def _tree(home: Path) -> list[str]:
    return sorted(str(p.relative_to(home)) for p in home.rglob("*"))


def test_a_directory_at_a_target_rejects_the_bundle_before_any_write(home: Path) -> None:
    (home / ".agent-context/global/commands/guide.md").mkdir(parents=True)
    before = _tree(home)
    with pytest.raises(R.BundleError):
        R.apply_bundle({"commands/a.md": "a", "commands/guide.md": "g", "commands/z.md": "z"}, home)
    assert _tree(home) == before


def test_a_file_where_a_parent_directory_belongs_rejects_the_bundle_before_any_write(
        home: Path) -> None:
    blocker = home / ".agent-context/global/skills/demo"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("not a directory")
    before = _tree(home)
    with pytest.raises(R.BundleError):
        R.apply_bundle({"commands/a.md": "a", "skills/demo/SKILL.md": "s"}, home)
    assert _tree(home) == before
    assert blocker.read_text() == "not a directory"


def test_a_blocked_bundle_keeps_the_old_cache_and_returns_false(
        home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    (home / ".agent-context/global/commands/guide.md").mkdir(parents=True)
    R.write_cache({"commands/old.md": "old"}, home)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN", "s3cret-token")
    monkeypatch.setattr(R, "fetch_bundle_mcp",
                        lambda url, token, timeout: {"commands/a.md": "a", "commands/guide.md": "g"})
    with caplog.at_level(logging.WARNING):
        assert R.materialize_on_start(home) is False
    assert not (home / ".agent-context/global/commands/a.md").exists()
    assert json.loads(R.cache_path(home).read_text()) == {"commands/old.md": "old"}


def test_a_case_only_rename_does_not_prune_the_file_just_written(home: Path) -> None:
    if not _case_insensitive(home):
        pytest.skip("needs a case-insensitive volume (macOS APFS default)")
    R.apply_bundle({"commands/file.md": "v1"}, home)
    R.write_cache({"commands/file.md": "v1"}, home)
    result = R.apply_bundle({"commands/File.md": "v2"}, home)
    assert result.pruned == []
    assert [p.read_text() for p in (home / ".agent-context/global/commands").iterdir()] == ["v2"]


def test_a_genuinely_dropped_entry_is_still_pruned_beside_a_surviving_one(home: Path) -> None:
    R.apply_bundle({"commands/keep.md": "k", "commands/drop.md": "d"}, home)
    R.write_cache({"commands/keep.md": "k", "commands/drop.md": "d"}, home)
    result = R.apply_bundle({"commands/keep.md": "k"}, home)
    assert result.pruned == ["commands/drop.md"]
    assert not R.target_for("commands/drop.md", home).exists()
    assert R.target_for("commands/keep.md", home).read_text() == "k"
