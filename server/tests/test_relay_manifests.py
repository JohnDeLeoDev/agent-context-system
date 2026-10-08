'C11 prerequisite (a): the relay bundle carries the two harness manifests.\n\n`harness-materialize.py` reads `~/.agent-context/global/mcp-servers.json` and\n`hooks-manifest.json`. A machine with no clone gets them only if `/materialized` carries them\nand the relay writes them to that path. Bundle keys are `manifests/<file>`.'

from __future__ import annotations

import stat
from pathlib import Path

import anyio
import pytest

from agent_context import relay_materialize as R
from agent_context.materialize import build_materialized_map

MCP = "manifests/mcp-servers.json"
HOOKS = "manifests/hooks-manifest.json"
MCP_TEXT = '{"servers": {"agent-context": {"command": "{RELAY}"}}, "note": "café"}\r\n'
HOOKS_TEXT = '{"PreToolUse": []}\n'
MANIFEST_KEYS = {MCP, HOOKS}


@pytest.fixture
def store(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    g = root / "global"
    (g / "hooks").mkdir(parents=True)
    (g / "scripts").mkdir()
    (g / "docs").mkdir()
    (g / "hooks" / "guard.py").write_text("print('guard')\n")
    (g / "scripts" / "tool.py").write_text("print('tool')\n")
    (g / "docs" / "guide.md").write_text("guide\n")
    (g / "mcp-servers.json").write_bytes(MCP_TEXT.encode())
    (g / "hooks-manifest.json").write_bytes(HOOKS_TEXT.encode())
    (g / "settings-seed.json").write_text("{}\n")  
    return root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h




def test_the_map_carries_both_manifests_byte_for_byte(store: Path) -> None:
    bundle = build_materialized_map(str(store))
    assert bundle[MCP] == MCP_TEXT
    assert bundle[HOOKS] == HOOKS_TEXT


def test_the_map_carries_no_other_global_json(store: Path) -> None:
    bundle = build_materialized_map(str(store))
    assert not [k for k in bundle if "settings-seed" in k]
    assert {k for k in bundle if k.startswith("manifests/")} == MANIFEST_KEYS


@pytest.mark.parametrize("missing, kept", [
    ("mcp-servers.json", HOOKS),
    ("hooks-manifest.json", MCP),
])
def test_a_missing_manifest_is_omitted_without_raising(
        store: Path, missing: str, kept: str) -> None:
    (store / "global" / missing).unlink()
    bundle = build_materialized_map(str(store))
    assert {k for k in bundle if k.startswith("manifests/")} == {kept}


def test_a_store_with_neither_manifest_still_builds(store: Path) -> None:
    (store / "global" / "mcp-servers.json").unlink()
    (store / "global" / "hooks-manifest.json").unlink()
    bundle = build_materialized_map(str(store))
    assert not [k for k in bundle if k.startswith("manifests/")]
    assert "hooks/guard.py" in bundle




def test_the_existing_keys_are_unchanged_by_the_manifests(store: Path) -> None:
    bundle = build_materialized_map(str(store))
    legacy = {k: v for k, v in bundle.items() if not k.startswith("manifests/")}
    assert legacy == {
        "hooks/guard.py": "print('guard')\n",
        "scripts/tool.py": "print('tool')\n",
    }


def test_existing_prefixes_keep_their_targets_and_modes(home: Path) -> None:
    bundle = {"hooks/guard.py": "x\n", "scripts/tool.py": "y\n", "commands/guide.md": "z\n"}
    R.apply_bundle(bundle, home)
    assert R.target_for("hooks/guard.py", home) == home / ".agent-context/global/hooks/guard.py"
    assert R.target_for("commands/guide.md", home) == home / ".agent-context/global/commands/guide.md"
    assert R.target_for("hooks/guard.py", home).stat().st_mode & stat.S_IXUSR
    assert not R.target_for("commands/guide.md", home).stat().st_mode & stat.S_IXUSR




@pytest.mark.parametrize("key, rel", [
    (MCP, ".agent-context/global/mcp-servers.json"),
    (HOOKS, ".agent-context/global/hooks-manifest.json"),
])
def test_manifest_keys_map_to_the_global_dir_where_harness_materialize_reads(
        home: Path, key: str, rel: str) -> None:
    assert R.target_for(key, home) == home / rel


def test_apply_writes_both_manifests_as_plain_files(home: Path) -> None:
    bundle = {MCP: MCP_TEXT, HOOKS: HOOKS_TEXT}
    result = R.apply_bundle(bundle, home)
    assert sorted(result.written) == sorted(MANIFEST_KEYS)
    assert R.target_for(MCP, home).read_bytes() == MCP_TEXT.encode()
    assert R.target_for(HOOKS, home).read_bytes() == HOOKS_TEXT.encode()
    for key in MANIFEST_KEYS:
        assert R.target_for(key, home).stat().st_mode & 0o777 == 0o644


def test_a_second_apply_leaves_the_manifests_alone(home: Path) -> None:
    bundle = {MCP: MCP_TEXT, HOOKS: HOOKS_TEXT}
    R.apply_bundle(bundle, home)
    result = R.apply_bundle(bundle, home)
    assert result.written == [] and sorted(result.unchanged) == sorted(MANIFEST_KEYS)




def test_a_dropped_manifest_is_pruned_and_nothing_else_in_global_is(home: Path) -> None:
    mine = home / ".agent-context/global/notes.txt"
    mine.parent.mkdir(parents=True)
    mine.write_text("mine\n")
    bundle = {MCP: MCP_TEXT, HOOKS: HOOKS_TEXT, "hooks/guard.py": "x\n"}
    R.apply_bundle(bundle, home)
    R.write_cache(bundle, home)
    result = R.apply_bundle({HOOKS: HOOKS_TEXT, "hooks/guard.py": "x\n"}, home)
    assert result.pruned == [MCP]
    assert not R.target_for(MCP, home).exists()
    assert R.target_for(HOOKS, home).read_text() == HOOKS_TEXT
    assert mine.read_text() == "mine\n"
    assert (home / ".agent-context/global/hooks").is_dir()
    assert (home / ".agent-context/global").is_dir()


def test_a_manifest_this_relay_never_wrote_is_not_pruned(home: Path) -> None:
    clone_file = home / ".agent-context/global/mcp-servers.json"
    clone_file.parent.mkdir(parents=True)
    clone_file.write_text("from a clone\n")
    R.apply_bundle({"docs/a.md": "a\n"}, home)
    R.write_cache({"docs/a.md": "a\n"}, home)
    R.apply_bundle({"docs/a.md": "a\n"}, home)
    assert clone_file.read_text() == "from a clone\n"




@pytest.mark.parametrize("key", [
    "manifests",
    "manifests/",
    "manifests/../escape.json",
    "manifests/a/../../../escape.json",
    "/manifests/mcp-servers.json",
])
def test_unsafe_manifest_keys_are_rejected(home: Path, key: str) -> None:
    with pytest.raises(R.BundleError):
        R.target_for(key, home)
    with pytest.raises(R.BundleError):
        R.apply_bundle({"docs/ok.md": "fine\n", key: "x"}, home)
    assert not R.target_for("docs/ok.md", home).exists()




@pytest.mark.parametrize("path", [
    "global/mcp-servers.json",
    "global/hooks-manifest.json",
])
def test_a_manifest_push_affects_the_bundle(path: str) -> None:
    assert R.affects_bundle([path]) is True


@pytest.mark.parametrize("path", [
    "global/settings-seed.json",
    "global/other.json",
    "projects/Proj/mcp-servers.json",
    "mcp-servers.json",
    "global/memories/a.md",
])
def test_other_paths_still_do_not_affect_the_bundle(path: str) -> None:
    assert R.affects_bundle([path]) is False


def test_existing_bundle_paths_still_affect_the_bundle() -> None:
    assert R.affects_bundle(["global/commands/guide.md"]) is True
    assert R.affects_bundle(["global/docs/guide.md"]) is False
    assert R.affects_bundle(["projects/Proj/commands/c.md"]) is True




def test_a_manifest_only_change_does_not_force_a_settings_sync() -> None:
    assert R.needs_settings_sync(R.ApplyResult(written=[MCP, HOOKS])) is False
    assert R.needs_settings_sync(R.ApplyResult(pruned=[MCP])) is False
    assert R.needs_settings_sync(R.ApplyResult(written=["hooks/guard.py"])) is True




def test_the_cache_fallback_applies_the_manifests(home: Path) -> None:
    R.write_cache({MCP: MCP_TEXT, HOOKS: HOOKS_TEXT}, home)
    assert R.apply_cached(home) is True
    assert R.target_for(MCP, home).read_bytes() == MCP_TEXT.encode()
    assert R.target_for(HOOKS, home).read_bytes() == HOOKS_TEXT.encode()


@pytest.mark.parametrize("path, wakes", [
    ("global/mcp-servers.json", True),
    ("global/hooks-manifest.json", True),
    ("global/settings-seed.json", False),
])
def test_the_refresher_wakes_for_a_manifest_push(home: Path, path: str, wakes: bool) -> None:
    async def scenario() -> bool:
        refresher = R.ContentRefresher(home=home)
        refresher.notify([path])
        return refresher._wake.is_set()

    assert anyio.run(scenario) is wakes
