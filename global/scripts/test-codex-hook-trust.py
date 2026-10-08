#!/usr/bin/env python3
'Acceptance tests for persistent, scoped Codex hook trust.'
import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).with_name("harness-materialize.py")


def load_materializer():
    spec = importlib.util.spec_from_file_location("harness_materialize", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    hm = load_materializer()
    path = "/Users/test/.codex/hooks.json"
    old = '''model = "example"

[hooks.state."/project/.codex/hooks.json:pre_tool_use:0:0"]
enabled = true
trusted_hash = "keep"

[hooks.state."/Users/test/.codex/hooks.json:session_start:0:0"]
enabled = true
trusted_hash = "old"

[tui]
status_line = []
'''
    states = [
        {"key": path + ":pre_tool_use:0:0", "currentHash": "sha256:first"},
        {"key": path + ":stop:0:0", "currentHash": "sha256:second"},
    ]
    new = hm.render_codex_trust(old, path, states)
    assert 'trusted_hash = "keep"' in new
    assert 'trusted_hash = "old"' not in new
    assert new.count("/Users/test/.codex/hooks.json:") == 2
    assert 'trusted_hash = "sha256:first"' in new
    assert 'trusted_hash = "sha256:second"' in new
    assert '[tui]\nstatus_line = []' in new
    again = hm.render_codex_trust(new, path, states)
    assert again == new
    print("test-codex-hook-trust: 1 passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
