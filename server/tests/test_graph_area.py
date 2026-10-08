'Area is a filterable Obsidian property derived from store scope.'

from agent_context.projects import upsert_project
from agent_context.store import ContextStore


def test_area_defaults_follow_workspace_and_global_scope(tmp_path):
    root = tmp_path / "ctx"
    (root / "global").mkdir(parents=True)
    store = ContextStore(root=str(root))
    upsert_project(store, "test:work", "Work", workspace="example-workspace")
    upsert_project(store, "test:home", "Home", workspace="personal")
    store.upsert("doc", "global.md", {"title": "Global"}, "global")
    store.upsert("doc", "work.md", {"title": "Work"}, "work", scope="project:Work")
    store.upsert("doc", "home.md", {"title": "Home"}, "home", scope="project:Home")
    store.upsert("doc", "workspace.md", {"title": "Workspace"}, "workspace", scope="ws:example-workspace")

    global_doc = store.get("doc", "global.md")
    work_doc = store.get("doc", "work.md", "Work")
    home_doc = store.get("doc", "home.md", "Home")
    workspace_doc = store.get("doc", "workspace.md", scope="ws:example-workspace")
    assert global_doc is not None and global_doc["area"] == "Agent Context"
    assert work_doc is not None and work_doc["area"] == "example-workspace"
    assert home_doc is not None and home_doc["area"] == "Personal"
    assert workspace_doc is not None and workspace_doc["area"] == "example-workspace"


def test_area_override_survives_upsert_without_body_change(store):
    first = store.upsert("doc", "mine.md", {"title": "Mine", "area": "Personal"}, "mine\n")
    second = store.upsert("doc", "mine.md", {"title": "Mine"}, body=None)
    assert first["area"] == second["area"] == "Personal"
    assert second["body"] == "mine"
