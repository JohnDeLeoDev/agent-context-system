'Regression for observation 634: manifest changes have one MCP task path.'
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_context import store_tasks


@pytest.fixture
def manifest_store(tmp_path):
    live = Path.home() / ".agent-context"
    (tmp_path / "global").mkdir()
    (tmp_path / "global" / "hooks").symlink_to(live / "global" / "hooks")
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "src").symlink_to(Path(__file__).parents[1] / "src")
    source = live / "global" / "mcp-servers.json"
    (tmp_path / "global" / "mcp-servers.json").write_bytes(source.read_bytes())
    return tmp_path


def invoke(root, *args, spec=None):
    script = Path.home() / ".agent-context" / "global" / "scripts" / "mcp-manifest.py"
    return subprocess.run(
        [sys.executable, str(script), *args],
        input=json.dumps(spec) if spec is not None else "",
        text=True, capture_output=True, timeout=10,
        env={**os.environ, "AGENT_CONTEXT_STORE": str(root)},
    )


def test_manifest_task_round_trip_and_preserves_other_entries(manifest_store):
    root = manifest_store
    before = json.loads(invoke(root, "read").stdout)
    spec = dict(before["servers"]["example-workspace-admin"])
    spec["url"] = "https://example.com/mcp"
    path = root / "global" / "mcp-servers.json"
    original = path.read_bytes()
    assert invoke(root, "upsert", "fixture", "--dry-run", spec=spec).returncode == 0
    assert path.read_bytes() == original
    assert invoke(root, "upsert", "fixture", spec=spec).returncode == 0
    after = json.loads(invoke(root, "read").stdout)
    assert after["servers"].pop("fixture") == spec
    assert after == before
    assert invoke(root, "remove", "fixture").returncode == 0
    assert json.loads(path.read_text()) == before
    assert invoke(root, "remove", "agent-context").returncode == 2
    assert "read" in store_tasks.TASKS["mcp-manifest"].readonly


@pytest.mark.parametrize("change", [
    {"transport": "sse"},
    {"url": "https://user:pass@example.com/mcp"},
    {"projects": ["../other"]},
    {"harnesses": ["unknown"]},
    {"headers": {"Authorization": "Bearer fixture"}},
    {"env": {"API_TOKEN": "a1b2c3d4e5f6g7h8i9j0"}},
    {"oauthCallbackPort": 12345},
])
def test_refuses_invalid_spec_without_writing(manifest_store, change):
    path = manifest_store / "global" / "mcp-servers.json"
    original = path.read_bytes()
    spec = dict(json.loads(original)["servers"]["example-workspace-admin"])
    spec.update(change)
    result = invoke(manifest_store, "upsert", "fixture", spec=spec)
    assert result.returncode == 2
    assert path.read_bytes() == original
    assert "a1b2c3d4e5f6g7h8i9j0" not in result.stderr
