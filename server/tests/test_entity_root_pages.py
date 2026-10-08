'Graph indexes in generated project and workspace root pages.'

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "global/scripts/entity-root-pages.py"


def generate(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "store"
    out = tmp_path / "out"
    (root / "machines").mkdir(parents=True)
    project = root / "projects/example-api"
    (project / "docs").mkdir(parents=True)
    (project / "skills/build/references").mkdir(parents=True)
    (project / "project.toml").write_text('workspace = "example-workspace"\n')
    (project / "docs/example-api.md").write_text(
        "# Old root\n\n## Notes\n- [[global/docs/shared|Shared reference]]\n"
    )
    (project / "docs/architecture.md").write_text("# Architecture\n")
    (project / "skills/build/SKILL.md").write_text("# Build\n")
    (project / "skills/build/references/usage.md").write_text("# Usage\n")
    workspace = root / "workspaces/example-workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs/standards.md").write_text("# Standards\n")
    (root / "global/docs").mkdir(parents=True)
    (root / "global/docs/shared.md").write_text("# Shared\n")
    (root / "global/docs/agent-context-store.md").write_text(
        "# Old root\n\n## Notes\n- keep this note\n"
    )
    (root / "global/memory").mkdir()
    (root / "global/memory/health.md").write_text("# Health\n")
    (root / "global/skills/triage").mkdir(parents=True)
    (root / "global/skills/triage/SKILL.md").write_text("# Triage\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(out), str(root)],
        capture_output=True,
        text=True,
        check=True,
    )
    manifest = json.loads(result.stdout)
    return {row["title"]: Path(row["body_path"]).read_text() for row in manifest}


def test_project_index_links_every_owned_markdown_and_preserves_notes(tmp_path: Path) -> None:
    pages = generate(tmp_path)
    page = pages["example-api"]
    assert "[[projects/example-api/docs/architecture|architecture]]" in page
    assert "[[projects/example-api/skills/build/SKILL|SKILL]]" in page
    assert "[[projects/example-api/skills/build/references/usage|usage]]" in page
    assert "[[projects/example-api/docs/example-api|example-api]]" not in page
    assert "[[global/docs/shared|Shared reference]]" in page
    assert page.count("[[global/docs/shared|Shared reference]]") == 1


def test_workspace_index_links_owned_markdown_and_project(tmp_path: Path) -> None:
    pages = generate(tmp_path)
    page = pages["example-workspace"]
    assert "[[workspaces/example-workspace/docs/standards|standards]]" in page
    assert "[[example-api]]" in page
    assert "[[global/docs/shared|shared]]" not in page


def test_global_index_links_every_owned_markdown_and_preserves_notes(tmp_path: Path) -> None:
    page = generate(tmp_path)["agent-context-store"]
    assert "[[global/docs/shared|shared]]" in page
    assert "[[global/memory/health|health]]" in page
    assert "[[global/skills/triage/SKILL|SKILL]]" in page
    assert "[[global/docs/agent-context-store|agent-context-store]]" not in page
    assert page.count("[[global/docs/shared|shared]]") == 1
    assert "## Notes\n- keep this note" in page
