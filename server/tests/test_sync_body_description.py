'upsert_skill(description=...) rewrites the body\'s own `description:` line. Written bare,\na description holding ": " broke the YAML the harness reads.'
import yaml

from agent_context import fstools as T
from agent_context.entities import sync_body_description


def _front(body):
    end = body.find("\n---", 4)
    return yaml.safe_load(body[4:end])


BODY = "---\nname: s\ndescription: old\nallowed-tools:\n  - Bash\n---\n\n# s\n"


def test_description_with_colon_space_parses():
    desc = "Deploy to a required target. No default: you must pass one."
    fm = _front(sync_body_description(BODY, desc))
    assert fm["description"] == desc
    assert fm["allowed-tools"] == ["Bash"]


def test_quotes_backslashes_and_non_ascii_round_trip():
    desc = "Say \"hi\" to C:\\tmp and café, then #tag: done"
    fm = _front(sync_body_description(BODY, desc))
    assert fm["description"] == desc


def test_folded_continuation_lines_still_dropped():
    body = "---\nname: s\ndescription: first\n  second\n  third\nother: x\n---\nbody\n"
    fm = _front(sync_body_description(body, "new: value"))
    assert fm == {"name": "s", "description": "new: value", "other": "x"}


def test_body_without_frontmatter_unchanged():
    assert sync_body_description("# no front\n", "a: b") == "# no front\n"


def test_upsert_skill_description_keeps_body_frontmatter_valid(store):
    T.upsert_skill(store, "sk", description="old", body=BODY)
    T.upsert_skill(store, "sk", description="No default: you must pass a target")
    body = T.get_entity(store, "skill", "sk")["body"]
    assert _front(body)["description"] == "No default: you must pass a target"
