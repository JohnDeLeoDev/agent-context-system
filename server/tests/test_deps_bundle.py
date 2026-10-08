"The fleet dependency manifest reaches relay-only machines through the materialized bundle.\n\ndeps-check.py arrives on a relay machine through the `scripts/` prefix. Its data file,\nglobal/deps/manifest.toml, had no bundle key, so the script ran there with nothing to read. The\nbundle now carries it as `manifests/deps/manifest.toml`, which the relay's existing writer lands\nat ~/.agent-context/global/deps/manifest.toml. A store without the file yields the bundle it\nalways did."
import json
from pathlib import Path

from agent_context import materialize, relay_materialize

KEY = "manifests/deps/manifest.toml"
MANIFEST = 'schema = 1\n[machine.box]\nos = "ubuntu"\nroles = ["base"]\n'


def _store(tmp_path, with_manifest):
    root = tmp_path / "store"
    (root / "global" / "scripts").mkdir(parents=True)
    (root / "global" / "scripts" / "deps-check.py").write_text("print('x')\n")
    (root / "global" / "mcp-servers.json").write_text("{}\n")
    if with_manifest:
        (root / "global" / "deps").mkdir()
        (root / "global" / "deps" / "manifest.toml").write_text(MANIFEST)
    return root


def test_the_bundle_carries_the_manifest_with_its_exact_content(tmp_path):
    bundle = materialize.build_materialized_map(str(_store(tmp_path, True)))
    assert bundle[KEY] == MANIFEST


def test_a_store_without_the_manifest_has_no_such_key(tmp_path):
    bundle = materialize.build_materialized_map(str(_store(tmp_path, False)))
    assert KEY not in bundle
    assert "manifests/mcp-servers.json" in bundle


def test_the_manifest_adds_one_key_and_changes_nothing_else(tmp_path):
    without = materialize.build_materialized_map(str(_store(tmp_path / "a", False)))
    with_it = materialize.build_materialized_map(str(_store(tmp_path / "b", True)))
    assert {k: v for k, v in with_it.items() if k != KEY} == without
    assert json.dumps(without, sort_keys=True) == json.dumps(
        {k: v for k, v in with_it.items() if k != KEY}, sort_keys=True)


def test_the_relay_writes_the_manifest_where_deps_check_reads_it(tmp_path):
    home = tmp_path / "home"
    bundle = materialize.build_materialized_map(str(_store(tmp_path, True)))
    result = relay_materialize.apply_bundle(bundle, home)
    target = home / ".agent-context" / "global" / "deps" / "manifest.toml"
    assert target.read_text() == MANIFEST
    assert oct(target.stat().st_mode & 0o777) == "0o644"
    assert KEY in result.written


def test_a_manifest_dropped_from_the_store_is_pruned_from_the_relay(tmp_path):
    home = tmp_path / "home"
    with_it = materialize.build_materialized_map(str(_store(tmp_path / "b", True)))
    relay_materialize.apply_bundle(with_it, home)
    relay_materialize.write_cache(with_it, home)
    without = materialize.build_materialized_map(str(_store(tmp_path / "a", False)))
    result = relay_materialize.apply_bundle(without, home)
    assert KEY in result.pruned
    assert not (home / ".agent-context" / "global" / "deps").exists()
    assert (home / ".agent-context" / "global" / "scripts" / "deps-check.py").is_file()


def test_an_older_relay_that_does_not_know_the_key_still_applies_the_rest(tmp_path):
    'The relay skips only an unknown FIRST segment; `manifests` is known, so an older relay\n    writes the new key as any other manifest file.'
    assert not relay_materialize._is_unknown_prefix(KEY)
    assert relay_materialize.target_for(KEY, Path("/h")) == Path(
        "/h/.agent-context/global/deps/manifest.toml")
