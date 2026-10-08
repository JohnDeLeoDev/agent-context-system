from pathlib import Path

from agent_context.graph import link_fields


class _Store:
    root = Path("/vault")

    def __init__(self, entities, by_key):
        self.entities = entities
        self.by_key = by_key

    def _ws_scope(self, _project):
        return None


def test_canonicalization_keeps_a_wikilink_anchor_and_alias():
    target = {
        "uuid": "target", "type": "memory", "scope": "global", "slug": "target",
        "_path": "/vault/global/memory/target.md",
    }
    store = _Store({"target": target}, {("memory", "global", "target"): "target"})

    fields, error = link_fields(
        {"depends_on": ["[[target#Heading|Read target]]"]}, key="source", kind="memory",
        scope="global", store=store,
    )

    assert error is None
    assert fields == {"depends_on": ["[[global/memory/target.md#Heading|Read target]]"]}


def test_global_source_canonicalizes_a_unique_target_in_another_scope():
    target = {
        "uuid": "target", "type": "memory", "scope": "project:Only", "slug": "target",
        "_path": "/vault/projects/Only/memory/target.md",
    }
    store = _Store({"target": target}, {("memory", "project:Only", "target"): "target"})

    fields, error = link_fields(
        {"depends_on": ["target"]}, key="source", kind="memory", scope="global", store=store,
    )

    assert error is None
    assert fields == {"depends_on": ["[[projects/Only/memory/target.md]]"]}


def test_extensionless_markdown_vault_path_canonicalizes_to_the_file_path():
    target = {
        "uuid": "target", "type": "doc", "scope": "project:Api", "path": "auth.md",
        "_path": "/vault/projects/Api/docs/auth.md",
    }
    store = _Store({"target": target}, {("doc", "project:Api", "auth.md"): "target"})

    fields, error = link_fields(
        {"depends_on": ["projects/Api/docs/auth"]}, key="source", kind="memory",
        scope="global", store=store,
    )

    assert error is None
    assert fields == {"depends_on": ["[[projects/Api/docs/auth.md]]"]}


def test_ambiguous_script_and_hook_target_stays_unqualified():
    script = {
        "uuid": "script", "type": "script", "scope": "global", "name": "worker",
        "_path": "/vault/global/scripts/worker.py",
    }
    hook = {
        "uuid": "hook", "type": "hook", "scope": "global", "name": "worker",
        "_path": "/vault/global/hooks/worker.py",
    }
    store = _Store(
        {"script": script, "hook": hook},
        {("script", "global", "worker"): "script", ("hook", "global", "worker"): "hook"},
    )

    fields, error = link_fields(
        {"depends_on": ["worker"]}, key="source", kind="memory", scope="global", store=store,
    )

    assert error is None
    assert fields == {"depends_on": ["[[worker]]"]}
