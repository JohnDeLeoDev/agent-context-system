#!/usr/bin/env python3
'Acceptance checks for the Codex projection of the agent-context store.'
import importlib.util
import json
import os
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("harness_materialize", HERE / "harness-materialize.py")
assert SPEC is not None and SPEC.loader is not None
HM = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HM)


def put(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def fixture(root):
    home = root / "home"
    store = home / ".agent-context"
    project = home / "project"
    (home / ".codex").mkdir(parents=True)
    put(store / "shared-skills" / "one" / "SKILL.md",
        "---\nname: one\ndescription: One skill.\n---\n\nOne body.\n")
    put(store / "global" / "commands" / "handoff.md",
        "---\ndescription: Transfer this task.\n---\n\nTransfer body.\n")
    put(store / "global" / "agents" / "worker-explore.md",
        "---\nname: worker-explore\ndescription: Explore.\nmodel: sonnet\n"
        "tools: Read, Grep\n---\n\nExplore body.\n")
    put(project / ".agents" / "claude" / "skills" / "two" / "SKILL.md",
        "---\nname: two\ndescription: Two skill.\n---\n\nTwo body.\n")
    put(project / ".agents" / "claude" / "commands" / "project-command.md",
        "# Project command\n\nProject body.\n")
    put(project / ".agents" / "claude" / "agents" / "worker-review.md",
        "---\nname: worker-review\ndescription: Review.\nmodel: sonnet\n---\n\nReview body.\n")
    return home, store, project


def test_global_and_project_content():
    assert hasattr(HM, "materialize_codex_content"), "Codex content projection is missing"
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as td:
        home, store, project = fixture(Path(td))
        report = {}
        HM.materialize_codex_content(str(home), str(store), str(project), report)
        assert (home / ".agents" / "skills" / "one" / "SKILL.md").exists()
        assert "Transfer this task" in (home / ".agents" / "skills" /
                                        "handoff" / "SKILL.md").read_text()
        assert (home / ".codex" / "prompts" / "handoff.md").exists()
        assert "worker-explore" in (home / ".codex" / "agents" /
                                    "worker-explore.toml").read_text()
        assert (project / ".agents" / "skills" / "two" / "SKILL.md").exists()
        assert (project / ".agents" / "skills" / "project-command" /
                "SKILL.md").exists()
        assert (project / ".codex" / "agents" / "worker-review.toml").exists()


def test_idempotence_retirement_and_foreign_files():
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as td:
        home, store, project = fixture(Path(td))
        foreign = home / ".agents" / "skills" / "foreign" / "SKILL.md"
        put(foreign, "Foreign skill\n")
        report = {}
        HM.materialize_codex_content(str(home), str(store), str(project), report)
        first = (home / ".agents" / "skills" / "handoff" / "SKILL.md").read_bytes()
        HM.materialize_codex_content(str(home), str(store), str(project), report)
        assert (home / ".agents" / "skills" / "handoff" / "SKILL.md").read_bytes() == first
        (store / "global" / "commands" / "handoff.md").unlink()
        HM.materialize_codex_content(str(home), str(store), str(project), report)
        assert not (home / ".agents" / "skills" / "handoff").exists()
        assert not (home / ".codex" / "prompts" / "handoff.md").exists()
        assert foreign.read_text() == "Foreign skill\n"


def test_mcp_preserves_manual_and_scopes_project():
    assert hasattr(HM, "materialize_codex_mcp"), "Codex MCP projection is missing"
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as td:
        home, store, project = fixture(Path(td))
        config = home / ".codex" / "config.toml"
        put(config, "[mcp_servers.manual]\ncommand = \"manual\"\n")
        manifest = {"servers": {
            "agent-context": {"transport": "stdio", "command": "/bin/context",
                              "args": [], "harnesses": ["claude", "codex"]},
            "project-tool": {"transport": "stdio", "command": "/bin/project",
                             "args": [], "projects": ["project"],
                             "harnesses": ["claude", "codex"]},
            "xcode": {"transport": "stdio", "command": "/bin/xcode", "args": [],
                      "projects": ["project"], "harnesses": ["claude"]},
        }}
        HM.materialize_codex_mcp(str(home), manifest, {})
        text = config.read_text()
        assert '[mcp_servers.manual]' in text
        assert '[mcp_servers.agent-context]' in text
        assert '[mcp_servers.project-tool]' not in text
        project_config = project / ".codex" / "config.toml"
        assert '[mcp_servers.project-tool]' in project_config.read_text()
        assert 'xcode' not in project_config.read_text()
        HM.materialize_codex_mcp(str(home), {"servers": {}}, {})
        assert '[mcp_servers.manual]' in config.read_text()
        assert '[mcp_servers.agent-context]' not in config.read_text()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"test-codex-parity: {len(tests)} passed")
