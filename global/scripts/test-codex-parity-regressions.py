#!/usr/bin/env python3
'Codex projection regressions found during adversarial review.'
import hashlib
import importlib.util
import json
import os
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("harness-materialize.py")
SPEC = importlib.util.spec_from_file_location("hm", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
HM = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HM)


def test_store_agent_metadata_and_read_only_sandbox():
    source = ("---\nuuid: test\nname: worker-explore\n"
              "description: Explore safely.\nmodel: sonnet\n---\n\nExplore.\n")
    native = HM._codex_native_source(source, "agent")
    rendered = HM._codex_agent("worker-explore", native)
    assert 'description = "Explore safely."' in rendered
    assert 'model = "gpt-5.6-terra"' in rendered
    assert 'sandbox_mode = "read-only"' in rendered


def test_owned_manifest_cannot_prune_outside_destination():
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as td:
        root = Path(td)
        dst = root / "skills"
        dst.mkdir()
        victim = root / "victim"
        victim.write_text("keep me")
        digest = hashlib.sha256(victim.read_bytes()).hexdigest()
        (dst / ".agent-context-owned.json").write_text(
            json.dumps({"../victim": digest}))
        HM._sync_codex_files(str(dst), {}, {}, "skills")
        assert victim.exists(), "manifest traversal deleted a file outside the projection"
        assert victim.read_text() == "keep me"


def test_mcp_env_table_is_declared_once():
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as td:
        config = Path(td) / "config.toml"
        HM._codex_mcp_config(str(config), {"sample": {
            "transport": "stdio", "command": "/bin/true", "args": [],
            "env": {"FIRST": "one", "SECOND": "two"}}})
        assert config.read_text().count("[mcp_servers.sample.env]") == 1


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"test-codex-parity-regressions: {len(tests)} passed")
