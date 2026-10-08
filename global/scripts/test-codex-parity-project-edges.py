#!/usr/bin/env python3
'Project edge cases for Codex materialization.'
import importlib.util
import json
import os
import tempfile
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "hm", Path(__file__).with_name("harness-materialize.py"))
assert SPEC is not None and SPEC.loader is not None
HM = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HM)


def test_legacy_skill_link_becomes_independent_projection():
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as td:
        home = Path(td)
        store = home / ".agent-context"
        project = home / "project"
        (home / ".codex").mkdir()
        claude_skills = project / ".claude" / "skills"
        claude_skills.mkdir(parents=True)
        (claude_skills / "source.txt").write_text("preserve")
        (project / ".agents" / "claude" / "commands").mkdir(parents=True)
        (project / ".agents" / "claude" / "commands" / "example.md").write_text(
            "# Example\n\nRun example.\n")
        (project / ".agents" / "skills").symlink_to("../.claude/skills")
        HM.materialize_codex_content(str(home), str(store), str(project), {})
        assert not (project / ".agents" / "skills").is_symlink(), \
            "Codex output still points into Claude's disposable projection"
        assert (project / ".agents" / "skills" / "example" / "SKILL.md").exists()
        assert (claude_skills / "source.txt").read_text() == "preserve"
        assert not (claude_skills / ".agent-context-owned.json").exists()


def test_mcp_only_repo_excludes_generated_codex_dir():
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as td:
        home = Path(td)
        project = home / "project"
        (home / ".codex").mkdir()
        info = project / ".git" / "info"
        info.mkdir(parents=True)
        (info / "exclude").write_text("old-pattern")
        manifest = {"servers": {"sample": {
            "transport": "stdio", "command": "/bin/true", "args": [],
            "projects": ["project"], "harnesses": ["codex"]}}}
        HM.materialize_codex_mcp(str(home), manifest, {})
        assert (info / "exclude").read_text().endswith("old-pattern\n.codex/\n"), \
            "generated Codex config remains untracked in the project"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"test-codex-parity-project-edges: {len(tests)} passed")
