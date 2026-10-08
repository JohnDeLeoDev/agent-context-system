'policy: an edit made in Obsidian, without MCP, never reaches git.\n\nContext graph T3 once made the sync loop commit such edits (`git add -A` over the tree).\npolicy reversed that: the one-pathway rule covers user too, so Obsidian is a read-only\nviewer and the daemon commits only what it wrote itself (the write ledger, policy). A hand\nedit stays in the worktree, uncommitted and unpushed, and is named in `outside_edits`\n(get_health), while an MCP write made beside it is committed and pushed as usual.'
import subprocess

import pytest
from fixture_signing import signing_config

from agent_context import memory
from agent_context.store import ContextStore, emit_frontmatter, stable_uuid


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture
def vault(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    root = tmp_path / "ctx"
    (root / "global" / "memory").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"),
                 *signing_config(tmp_path)):
        _git(root, "config", k, v)
    uid = stable_uuid("memory", "global", "kept")
    (root / "global" / "memory" / "kept.md").write_text(emit_frontmatter(
        {"uuid": uid, "type": "memory", "slug": "kept", "memory_type": "reference",
         "description": "d"}, "original body"))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-q", "origin", "main")
    return root, origin, ContextStore(root=str(root))


def _hand_edit(root):
    kept = root / "global" / "memory" / "kept.md"
    kept.write_text(kept.read_text().replace("original body", "edited in Obsidian"))
    note = root / "global" / "docs" / "notes" / "new-note.md"
    note.parent.mkdir(parents=True)
    note.write_text("A note made in Obsidian\n")


def test_hand_edits_are_never_committed_or_pushed(vault):
    root, origin, st = vault
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    _hand_edit(root)

    st.sync(push=True)

    assert _git(root, "rev-parse", "HEAD").stdout.strip() == head
    assert _git(origin, "rev-parse", "main").stdout.strip() == head
    assert "original body" in _git(origin, "show", "main:global/memory/kept.md").stdout
    assert st.outside_edits == ["global/docs/notes/new-note.md", "global/memory/kept.md"]
    
    assert "edited in Obsidian" in (root / "global" / "memory" / "kept.md").read_text()


def test_an_mcp_write_beside_a_hand_edit_is_committed_alone(vault):
    root, origin, st = vault
    _hand_edit(root)
    memory.upsert_memory(st, "via-mcp", "reference", "d", "written through the store")

    res = st.sync(push=True)

    assert res.get("push") is True, res
    tracked = _git(root, "ls-files").stdout.split()
    assert "global/memory/via-mcp.md" in tracked
    assert "global/docs/notes/new-note.md" not in tracked
    assert "original body" in _git(origin, "show", "main:global/memory/kept.md").stdout
    assert st.outside_edits == ["global/docs/notes/new-note.md", "global/memory/kept.md"]
