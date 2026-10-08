'lsp-canary.py, the lsp-failure-tripwire hook and preflight-core-health read\n~/.agent-context/global/lsp-canaries.json on every machine. It had no bundle key, so a relay\nmachine, where most LSP work happens, never knew which canaries to expect.'
from agent_context import materialize, relay_materialize

KEY = "manifests/lsp-canaries.json"
CONFIG = '{"servers": {}}\n'


def _store(tmp_path, with_config):
    root = tmp_path / "store"
    (root / "global" / "scripts").mkdir(parents=True)
    (root / "global" / "mcp-servers.json").write_text("{}\n")
    if with_config:
        (root / "global" / "lsp-canaries.json").write_text(CONFIG)
    return root


def test_the_bundle_carries_the_canary_config(tmp_path):
    assert materialize.build_materialized_map(str(_store(tmp_path, True)))[KEY] == CONFIG


def test_a_store_without_it_has_no_such_key(tmp_path):
    assert KEY not in materialize.build_materialized_map(str(_store(tmp_path, False)))


def test_the_relay_writes_it_where_the_readers_look(tmp_path):
    bundle = materialize.build_materialized_map(str(_store(tmp_path, True)))
    relay_materialize.apply_bundle(bundle, tmp_path / "home")
    target = tmp_path / "home" / ".agent-context" / "global" / "lsp-canaries.json"
    assert target.read_text() == CONFIG
