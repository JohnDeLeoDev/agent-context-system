'Context graph T5 item 4: home-materialize drops relation keys from projected skills and\nagent definitions.'
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "global" / "scripts" / "home-materialize.py"
TYPED = ("supersedes", "part_of", "sibling", "enforced_by", "contradicts")


@pytest.fixture(scope="module")
def hm():
    if not SCRIPT.is_file():
        pytest.skip(f"global/scripts/home-materialize.py is not in this checkout ({SCRIPT})")
    spec = importlib.util.spec_from_file_location("home_materialize_t5", SCRIPT)
    assert spec is not None and spec.loader is not None, SCRIPT
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _store_file(path, meta, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", *(f"{k}: {json.dumps(v)}" for k, v in meta.items()), "---", "", body]
    path.write_text("\n".join(lines), encoding="utf-8")


def _relations():
    return {rel: [f"[[target-{rel}]]"] for rel in TYPED}


def test_a_projected_skill_keeps_name_and_description_and_drops_relation_keys(hm, tmp_path):
    p = tmp_path / "skills" / "sk" / "SKILL.md"
    _store_file(p, {"uuid": "u-1", "type": "skill", "name": "sk", "description": "a skill",
                    "allowed_tools": "Read", **_relations()}, "# Skill\n\nsteps\n")
    hm.strip_store_frontmatter(str(p))
    out = p.read_text(encoding="utf-8")
    for rel in TYPED:
        assert rel not in out, out
    head = out.split("---")[1]
    assert "name:" in head and "description:" in head, out


def test_a_projected_agent_definition_drops_relation_keys(hm, tmp_path):
    p = tmp_path / "agents" / "worker.md"
    _store_file(p, {"uuid": "u-2", "type": "agent_definition", "name": "worker",
                    "description": "a worker", "model": "sonnet", "tools": "Read",
                    **_relations()}, "You are a worker.\n")
    hm.strip_store_frontmatter(str(p))
    out = p.read_text(encoding="utf-8")
    for rel in TYPED:
        assert rel not in out, out
    head = out.split("---")[1]
    assert "model:" in head and "description:" in head, out
