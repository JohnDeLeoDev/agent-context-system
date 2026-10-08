'global/node-tools/ reaches relay-only machines through the materialized bundle.'
from agent_context import materialize, relay_materialize

FILES = {
    "package.json": '{"dependencies": {"copilot-api": "latest"}}\n',
    "pnpm-workspace.yaml": "packages: []\n",
    "patches/copilot-api.patch": "--- a\n+++ b\n",
}


def _store(tmp_path, with_tree):
    root = tmp_path / "store"
    (root / "global" / "scripts").mkdir(parents=True)
    (root / "global" / "mcp-servers.json").write_text("{}\n")
    if with_tree:
        for rel, text in FILES.items():
            path = root / "global" / "node-tools" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    return root


def test_the_bundle_carries_the_whole_tree(tmp_path):
    bundle = materialize.build_materialized_map(str(_store(tmp_path, True)))
    got = {k[len("manifests/node-tools/"):]: v for k, v in bundle.items()
           if k.startswith("manifests/node-tools/")}
    assert got == FILES


def test_a_store_without_it_has_no_such_keys(tmp_path):
    bundle = materialize.build_materialized_map(str(_store(tmp_path, False)))
    assert not [k for k in bundle if k.startswith("manifests/node-tools/")]


def test_the_relay_writes_it_where_node_tools_sync_looks(tmp_path):
    bundle = materialize.build_materialized_map(str(_store(tmp_path, True)))
    relay_materialize.apply_bundle(bundle, tmp_path / "home")
    target = tmp_path / "home" / ".agent-context" / "global" / "node-tools"
    for rel, text in FILES.items():
        assert (target / rel).read_text() == text
