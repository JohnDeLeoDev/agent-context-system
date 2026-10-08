'get_materialized is global-only unless the caller sends project evidence.\n\n`project=<uuid>` and `remote=<url>` (repeatable, at most 8 in total) name the checkouts a\nmachine holds. The server resolves each with the existing project-resolve logic and adds\n`projects/<p>/...` for the resolved projects plus `workspaces/<ws>/...` for the workspace\ntheir project.toml names. Evidence only selects key groups: it is never a path, never read\nfrom disk. The bearer check that used to gate the equivalent GET /materialized route is\ncovered over a real MCP session in test_relay_get_materialized_tool.py; a direct call of\nthe tool function, as this file does, has no transport layer to enforce it.'
from __future__ import annotations

import builtins
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from agent_context import server as S
from agent_context.materialize import build_materialized_map
from agent_context.store import ContextStore, stable_uuid

FRONTMATTER_DOC = '---\nuuid: "policy"\ntype: "doc"\ntitle: "T"\n---\n\nbacking doc\n'


PROJECTS: dict[str, tuple[str, str | None]] = {
    "Alpha": ("github.com:org/alpha", "ws1"),
    "Beta": ("github.com:org/beta", "ws1"),
    "Gamma": ("github.com:org/gamma", "ws2"),
    "Solo": ("github.com:org/solo", None),
}
PROJECT_IDS = {name: stable_uuid("project", "global", remote) for name, (remote, _) in PROJECTS.items()}


def _put(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    _put(root / "global" / "scripts" / "g.py", "print('g')\n")
    _put(root / "global" / "hooks" / "g.sh", "#!/bin/bash\necho g\n")
    _put(root / "global" / "docs" / "g.md", FRONTMATTER_DOC)
    _put(root / "global" / "mcp-servers.json", "{}\n")
    for name, (remote, workspace) in PROJECTS.items():
        lines = [f'uuid = "{PROJECT_IDS[name]}"', 'type = "project"', f'display_name = "{name}"',
                 f'canonical_remote = "{remote}"']
        if workspace:
            lines.append(f'workspace = "{workspace}"')
        base = root / "projects" / name
        _put(base / "project.toml", "\n".join(lines) + "\n")
        _put(base / "skills" / "s" / "SKILL.md", f"skill of {name}\n")
        _put(base / "commands" / "cmd.md", f"cmd of {name}\n")
        _put(base / "docs" / "cmd.md", FRONTMATTER_DOC)
        _put(base / "scripts" / "run.py", "print('run')\n")
    for workspace in ("ws1", "ws2"):
        base = root / "workspaces" / workspace
        _put(base / "workspace.toml", f'name = "{workspace}"\n')
        _put(base / "skills" / "w" / "SKILL.md", f"skill of {workspace}\n")
        _put(base / "commands" / "w.md", f"cmd of {workspace}\n")
    return root


@pytest.fixture
def srv(monkeypatch: pytest.MonkeyPatch, store_root: Path):
    context_store = ContextStore(root=str(store_root))
    monkeypatch.setattr(S, "_get_conn", lambda: context_store)
    return S


def _scoped(key: str) -> bool:
    return key.startswith(("projects/", "workspaces/"))


def _global_only(store_root: Path) -> dict[str, str]:
    return {k: v for k, v in build_materialized_map(str(store_root)).items() if not _scoped(k)}


def _expected(store_root: Path, projects: list[str], workspaces: list[str]) -> dict[str, str]:
    full = build_materialized_map(str(store_root))
    prefixes = tuple([f"projects/{p}/" for p in projects] + [f"workspaces/{w}/" for w in workspaces])
    return {**_global_only(store_root), **{k: v for k, v in full.items() if k.startswith(prefixes)}}


def _fetch(srv, project: list[str] | None = None, remote: list[str] | None = None) -> dict[str, str]:
    out = json.loads(srv.get_materialized(project=project, remote=remote))
    assert "error" not in out, out
    return out




def test_no_evidence_returns_no_project_or_workspace_key(srv) -> None:
    bundle = _fetch(srv)
    scoped = sorted(k for k in bundle if _scoped(k))
    assert scoped == [], f"scoped keys leaked into the global bundle: {scoped[:5]}"


def test_no_evidence_is_byte_identical_to_build_materialized_map_minus_scoped_keys(
        srv, store_root: Path) -> None:
    assert _fetch(srv) == _global_only(store_root)


def test_no_evidence_carries_no_docs(srv) -> None:
    bundle = _fetch(srv)
    assert not [k for k in bundle if k.startswith("docs/")]
    assert "scripts/g.py" in bundle


def test_build_materialized_map_still_includes_scoped_keys(store_root: Path) -> None:
    full = build_materialized_map(str(store_root))
    assert "projects/Alpha/project.toml" in full
    assert "workspaces/ws1/workspace.toml" in full


def test_no_token_configured_is_still_global_only(srv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN", raising=False)
    bundle = _fetch(srv)
    assert not [k for k in bundle if _scoped(k)]




def test_project_uuid_selects_that_project_and_its_workspace(srv, store_root: Path) -> None:
    bundle = _fetch(srv, project=[PROJECT_IDS["Gamma"]])
    assert bundle == _expected(store_root, ["Gamma"], ["ws2"])


def test_remote_url_selects_that_project_and_its_workspace(srv, store_root: Path) -> None:
    bundle = _fetch(srv, remote=["git@github.com:org/gamma.git"])
    assert bundle == _expected(store_root, ["Gamma"], ["ws2"])


def test_https_remote_spelling_selects_the_same_project(srv, store_root: Path) -> None:
    bundle = _fetch(srv, remote=["https://github.com/org/gamma.git"])
    assert bundle == _expected(store_root, ["Gamma"], ["ws2"])


def test_project_without_workspace_adds_no_workspace_keys(srv, store_root: Path) -> None:
    bundle = _fetch(srv, project=[PROJECT_IDS["Solo"]])
    assert bundle == _expected(store_root, ["Solo"], [])


def test_unknown_evidence_selects_nothing_and_answers_no_error(srv, store_root: Path) -> None:
    bundle = _fetch(srv, project=["00000000-0000-0000-0000-000000000000"],
                    remote=["git@github.com:org/nothing.git"])
    assert bundle == _global_only(store_root)


def test_eight_evidence_values_are_accepted(srv, store_root: Path) -> None:
    remotes = [f"git@h:o/unknown{i}.git" for i in range(7)]
    bundle = _fetch(srv, project=[PROJECT_IDS["Gamma"]], remote=remotes)
    assert bundle == _expected(store_root, ["Gamma"], ["ws2"])


def test_nine_evidence_values_are_refused_with_an_error(srv) -> None:
    remotes = [f"git@h:o/unknown{i}.git" for i in range(8)]
    raw = srv.get_materialized(project=[PROJECT_IDS["Gamma"]], remote=remotes)
    out = json.loads(raw)
    assert "error" in out
    assert "projects/" not in raw


def test_the_cap_counts_project_and_remote_values_together(srv) -> None:
    projects = [f"00000000-0000-0000-0000-00000000000{i}" for i in range(5)]
    remotes = [f"git@h:o/unknown{i}.git" for i in range(4)]
    out = json.loads(srv.get_materialized(project=projects, remote=remotes))
    assert "error" in out




HOSTILE_VALUES = ["../../HOSTILE-A", "/etc/HOSTILE-B", "line1\nHOSTILE-C", "HOSTILE-" + "x" * 3000,
                  "ünï-HOSTILE-D", "..%2f..%2fHOSTILE-E", "projects/Alpha", "workspaces/ws1", "Alpha"]


@pytest.mark.parametrize("name", ["project", "remote"])
@pytest.mark.parametrize("value", HOSTILE_VALUES, ids=[f"hostile{i}" for i in range(len(HOSTILE_VALUES))])
def test_hostile_evidence_selects_nothing(srv, store_root: Path,
                                          name: str, value: str) -> None:
    kw = {name: [value]}
    raw = srv.get_materialized(**kw)
    out = json.loads(raw)
    if "error" not in out:
        assert out == _global_only(store_root)
    else:
        assert "projects/" not in raw


def test_evidence_is_never_used_to_touch_the_filesystem(
        srv, monkeypatch: pytest.MonkeyPatch) -> None:
    touched: list[str] = []

    def spy(real):
        def wrapper(*args: object, **kwargs: object) -> object:
            if args and isinstance(args[0], (str, bytes, os.PathLike)):
                touched.append(os.fsdecode(args[0]))
            return real(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(builtins, "open", spy(builtins.open))
    for target in ("listdir", "walk", "scandir"):
        monkeypatch.setattr(os, target, spy(getattr(os, target)))
    for target in ("isdir", "isfile", "exists"):
        monkeypatch.setattr(os.path, target, spy(getattr(os.path, target)))
    values = HOSTILE_VALUES[:5]
    srv.get_materialized(project=list(values), remote=list(values))
    assert touched, "spy saw no filesystem access at all"
    assert not [p for p in touched if "HOSTILE" in p], [p for p in touched if "HOSTILE" in p][:3]




def test_two_projects_in_one_workspace_evidence_for_one(srv, store_root: Path) -> None:
    bundle = _fetch(srv, project=[PROJECT_IDS["Alpha"]])
    assert bundle == _expected(store_root, ["Alpha"], ["ws1"])
    assert not [k for k in bundle if k.startswith("projects/Beta/")]


def test_two_projects_in_one_workspace_evidence_for_both(srv, store_root: Path) -> None:
    bundle = _fetch(srv, project=[PROJECT_IDS["Alpha"]], remote=["git@github.com:org/beta.git"])
    assert bundle == _expected(store_root, ["Alpha", "Beta"], ["ws1"])
    assert sorted(k for k in bundle if k.startswith("workspaces/")) == [
        "workspaces/ws1/commands/w.md", "workspaces/ws1/skills/w/SKILL.md",
        "workspaces/ws1/workspace.toml"]


def test_projects_in_two_workspaces_include_both_workspaces(srv, store_root: Path) -> None:
    bundle = _fetch(srv, project=[PROJECT_IDS["Alpha"], PROJECT_IDS["Gamma"]])
    assert bundle == _expected(store_root, ["Alpha", "Gamma"], ["ws1", "ws2"])
