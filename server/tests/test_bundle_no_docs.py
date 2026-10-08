'GET /materialized carries no `docs/` key (memory mcp-only-no-local-files).\n\nDocs are read over MCP with get_doc, so the bundle no longer ships global docs under\n`docs/<name>` or project command-backing docs under `docs/<project>/<name>`. `GET /doc`\nstill serves a global doc on its own, and a relay still applies a bundle that has no\n`docs/` key, as well as one from an older server that still has some.'
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from agent_context import relay_materialize as R
from agent_context.materialize import build_materialized_map, read_global_doc

FRONTMATTER_DOC = '---\nuuid: "policy"\ntype: "doc"\ntitle: "T"\n---\n\nbody\n'


def _put(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


@pytest.fixture
def store(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    _put(root / "global" / "hooks" / "guard.py", "print('guard')\n")
    _put(root / "global" / "commands" / "run.md", "run\n")
    _put(root / "global" / "docs" / "guide.md", FRONTMATTER_DOC)
    _put(root / "global" / "docs" / "nested" / "deep.md", "deep\n")
    project = root / "projects" / "Alpha"
    _put(project / "project.toml", 'uuid = "p-1"\ntype = "project"\ndisplay_name = "Alpha"\n')
    _put(project / "commands" / "cmd.md", "cmd\n")
    _put(project / "docs" / "cmd.md", FRONTMATTER_DOC)
    _put(project / "docs" / "cmd-extra.md", FRONTMATTER_DOC)
    return root


def test_bundle_has_no_docs_key(store: Path) -> None:
    bundle = build_materialized_map(str(store))
    assert not [k for k in bundle if k.startswith("docs/")], sorted(bundle)


def test_bundle_still_carries_the_other_global_keys(store: Path) -> None:
    bundle = build_materialized_map(str(store))
    assert bundle["hooks/guard.py"] == "print('guard')\n"
    assert bundle["commands/run.md"] == "run\n"


def test_project_docs_are_not_shipped_under_any_key(store: Path) -> None:
    bundle = build_materialized_map(str(store))
    assert not [k for k in bundle if "/docs/" in k or k.startswith("docs/")], sorted(bundle)
    assert "projects/Alpha/commands/cmd.md" in bundle


def test_the_doc_route_reader_still_serves_a_global_doc(store: Path) -> None:
    assert read_global_doc(str(store), "guide.md") == "body\n"
    assert read_global_doc(str(store), "nested/deep.md") == "deep\n"


def test_relay_applies_a_bundle_without_docs(store: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    bundle = {k: v for k, v in build_materialized_map(str(store)).items()
              if not k.startswith(("projects/", "workspaces/"))}
    result = R.apply_bundle(bundle, home)
    assert "hooks/guard.py" in result.written
    assert R.target_for("hooks/guard.py", home).read_text() == "print('guard')\n"
    assert not (home / ".agent-context" / "shared-docs").exists()


def test_a_project_docs_push_no_longer_affects_the_bundle() -> None:
    assert R.affects_bundle(["projects/Alpha/docs/cmd.md"]) is False
    assert R.affects_bundle(["projects/Alpha/commands/cmd.md"]) is True
