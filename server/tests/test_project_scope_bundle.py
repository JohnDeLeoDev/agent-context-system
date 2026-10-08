'Project-scope and workspace-scope files in the /materialized bundle.\n\n`build_materialized_map` adds `projects/<p>/<rel>` for project.toml and files under skills/,\ncommands/, agents/, scripts/, hooks/, parity/, ship/, and `workspaces/<ws>/<rel>` for\nworkspace.toml and files under skills/, commands/. Content is the RAW store file: store\nfrontmatter stays, `.meta.toml` / `.meta.json` sidecars are included. Never docs/, memory/,\ninstructions/ or any other subdir. Existing keys are unchanged.'
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_context.materialize import build_materialized_map

FRONTMATTER_SKILL = ('---\nuuid: "s-1"\ntype: "skill"\nname: "x"\ndescription: "d"\n---\n'
                     '\n## Body\n')
FRONTMATTER_CMD = '---\nuuid: "c-1"\ntype: "command"\nname: "p-cmd"\n---\n\necho proj\n'
FRONTMATTER_AGENT = ('---\nuuid: "a-1"\ntype: "agent_definition"\nname: "w"\n'
                     'description: "d"\npermission_mode: "plan"\n---\n\nbody\n')
BACKING_DOC = '---\nuuid: "policy"\ntype: "doc"\ntitle: "T"\n---\n\nbacking doc\n'

PROJECT_FILES = {
    "project.toml": 'name = "p"\n',
    "skills/x/SKILL.md": FRONTMATTER_SKILL,
    "skills/x/SKILL.md.meta.toml": 'type = "skill"\n',
    "skills/x/helper.py": "x = 1\n",
    "commands/p-cmd.md": FRONTMATTER_CMD,
    "commands/p-cmd.md.meta.toml": 'type = "command"\n',
    "agents/w.md": FRONTMATTER_AGENT,
    "scripts/run.py": "#!/usr/bin/env python3\nprint('run')\n",
    "scripts/run.py.meta.toml": 'executable = true\n',
    "scripts/run.py.meta.json": '{"executable": true}\n',
    "hooks/guard.sh": "#!/bin/bash\necho guard\n",
    "hooks/guard.sh.meta.toml": 'executable = true\n',
    "parity/parity.md": "parity\n",
    "ship/ship.md": "ship\n",
}
PROJECT_EXCLUDED = {
    "docs/p-cmd.md": BACKING_DOC,
    "docs/other.md": BACKING_DOC,
    "memory/m.md": "memory\n",
    "instructions/i.md": "instructions\n",
    "audit/a.md": "audit\n",
    "notes.md": "stray top-level file\n",
    "scripts/__pycache__/run.cpython-312.pyc": "junk\n",
    "scripts/.DS_Store": "junk\n",
    "skills/.DS_Store": "junk\n",
}
WORKSPACE_FILES = {
    "workspace.toml": 'name = "ws"\n',
    "skills/wx/SKILL.md": FRONTMATTER_SKILL,
    "skills/wx/SKILL.md.meta.toml": 'type = "skill"\n',
    "commands/ws-cmd.md": FRONTMATTER_CMD,
}
WORKSPACE_EXCLUDED = {
    "docs/d.md": BACKING_DOC,
    "memory/m.md": "memory\n",
    "instructions/i.md": "instructions\n",
    "agents/w.md": FRONTMATTER_AGENT,
    "scripts/s.py": "print(1)\n",
    "hooks/h.sh": "echo\n",
}


def _put(base: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")


@pytest.fixture
def store(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    _put(root / "global", {
        "hooks/g.sh": "#!/bin/bash\necho g\n",
        "scripts/g.py": "print('g')\n",
        "skills/gs/SKILL.md": FRONTMATTER_SKILL,
        "commands/g.md": FRONTMATTER_CMD,
        "agents/g.md": FRONTMATTER_AGENT,
        "docs/g.md": BACKING_DOC,
        "mcp-servers.json": "{}\n",
        "hooks-manifest.json": "{}\n",
    })
    _put(root / "projects" / "p", {**PROJECT_FILES, **PROJECT_EXCLUDED})
    _put(root / "workspaces" / "ws", {**WORKSPACE_FILES, **WORKSPACE_EXCLUDED})
    return root


def test_project_files_are_bundled_raw_under_projects_prefix(store: Path) -> None:
    bundle = build_materialized_map(str(store))
    for rel, content in PROJECT_FILES.items():
        key = f"projects/p/{rel}"
        assert key in bundle, f"missing bundle key {key}"
        assert bundle[key] == content, f"{key} is not the raw store file"


def test_workspace_files_are_bundled_raw_under_workspaces_prefix(store: Path) -> None:
    bundle = build_materialized_map(str(store))
    for rel, content in WORKSPACE_FILES.items():
        key = f"workspaces/ws/{rel}"
        assert key in bundle, f"missing bundle key {key}"
        assert bundle[key] == content, f"{key} is not the raw store file"


def test_store_frontmatter_is_not_stripped_in_project_scope(store: Path) -> None:
    bundle = build_materialized_map(str(store))
    assert bundle.get("projects/p/skills/x/SKILL.md", "") .startswith('---\nuuid: "s-1"')
    assert 'uuid: "a-1"' in bundle.get("projects/p/agents/w.md", "")
    assert 'uuid: "c-1"' in bundle.get("workspaces/ws/commands/ws-cmd.md", "")


def test_sidecars_are_included_in_project_scope(store: Path) -> None:
    bundle = build_materialized_map(str(store))
    for key in ("projects/p/skills/x/SKILL.md.meta.toml", "projects/p/scripts/run.py.meta.toml",
                "projects/p/scripts/run.py.meta.json", "projects/p/hooks/guard.sh.meta.toml",
                "workspaces/ws/skills/wx/SKILL.md.meta.toml"):
        assert key in bundle, f"sidecar {key} missing"


def test_project_and_workspace_junk_and_other_subdirs_never_bundled(store: Path) -> None:
    bundle = build_materialized_map(str(store))
    scoped = [k for k in bundle if k.startswith(("projects/", "workspaces/"))]
    assert scoped, "no projects/ or workspaces/ keys at all"
    for key in scoped:
        assert "__pycache__" not in key and not key.endswith(".DS_Store"), key
    for rel in PROJECT_EXCLUDED:
        assert f"projects/p/{rel}" not in bundle, rel
    for rel in WORKSPACE_EXCLUDED:
        assert f"workspaces/ws/{rel}" not in bundle, rel


def test_only_allowed_subdirs_appear_in_scoped_keys(store: Path) -> None:
    bundle = build_materialized_map(str(store))
    project_dirs = {"skills", "commands", "agents", "scripts", "hooks", "parity", "ship"}
    workspace_dirs = {"skills", "commands"}
    scoped = [k for k in bundle if k.startswith(("projects/", "workspaces/"))]
    assert scoped, "no projects/ or workspaces/ keys at all"
    for key in scoped:
        parts = key.split("/")
        allowed = project_dirs if parts[0] == "projects" else workspace_dirs
        top = "project.toml" if parts[0] == "projects" else "workspace.toml"
        assert parts[2] == top or parts[2] in allowed, key


def test_existing_keys_are_unchanged(store: Path) -> None:
    bundle = build_materialized_map(str(store))
    existing = {k: v for k, v in bundle.items()
                if not k.startswith(("projects/", "workspaces/"))}
    assert set(existing) == {
        "hooks/g.sh", "scripts/g.py", "skills/gs/SKILL.md", "commands/g.md", "agents/g.md",
        "manifests/mcp-servers.json", "manifests/hooks-manifest.json"}
    assert existing["hooks/g.sh"] == "#!/bin/bash\necho g\n"
    assert "uuid" not in existing["skills/gs/SKILL.md"]


def test_non_utf8_project_file_is_skipped_without_failing_the_build(store: Path) -> None:
    (store / "projects" / "p" / "scripts" / "blob.bin").write_bytes(b"\xff\xfe\x00\x80")
    (store / "workspaces" / "ws" / "skills" / "wx" / "blob.bin").write_bytes(b"\xff\xfe\x00")
    bundle = build_materialized_map(str(store))
    assert "projects/p/scripts/blob.bin" not in bundle
    assert "workspaces/ws/skills/wx/blob.bin" not in bundle
    assert "projects/p/scripts/run.py" in bundle, "valid files must still be bundled"


def test_project_without_optional_dirs_and_files_in_place_of_dirs(store: Path) -> None:
    (store / "projects" / "bare").mkdir()
    (store / "projects" / "bare" / "project.toml").write_text('name = "bare"\n')
    (store / "projects" / "README.md").write_text("not a project\n")
    bundle = build_materialized_map(str(store))
    assert bundle.get("projects/bare/project.toml") == 'name = "bare"\n'
    assert not any(k.startswith("projects/README") for k in bundle)


def test_scoped_keys_use_posix_separators_and_no_absolute_paths(store: Path) -> None:
    bundle = build_materialized_map(str(store))
    scoped = [k for k in bundle if k.startswith(("projects/", "workspaces/"))]
    assert scoped, "no projects/ or workspaces/ keys at all"
    for key in scoped:
        assert not os.path.isabs(key) and "\\" not in key and ".." not in key.split("/"), key
