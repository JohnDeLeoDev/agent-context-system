'Scripts and hooks share the memory description limit: refused on write, reported by check_integrity.'
import pytest

from agent_context import fstools as T
from agent_context.index import _DESC_MAX


def _raw(store, kind, name, description):
    'Write below the guard, as an entity stored before the limit existed.'
    fields = {"language": "sh", "origin": "user", "description": description}
    if kind == "hook":
        fields.update(event_type="Stop", timeout_seconds=30)
    store.upsert(kind, name, fields, body="echo hi\n")


def _upsert(kind, store, name, **kw):
    if kind == "hook":
        return T.upsert_hook(store, name, event_type="Stop", script_body="echo hi\n", **kw)
    return T.upsert_script(store, name, script_body="echo hi\n", **kw)


@pytest.mark.parametrize("kind", ["script", "hook"])
def test_description_over_limit_is_refused(store, kind):
    out = _upsert(kind, store, "big", description="x" * (_DESC_MAX + 1))
    assert "error" in out
    assert str(_DESC_MAX) in out["error"]
    assert "Nothing was written" in out["error"]
    assert store.get(kind, "big", None) is None


@pytest.mark.parametrize("kind", ["script", "hook"])
def test_description_at_limit_passes(store, kind):
    out = _upsert(kind, store, "edge", description="x" * _DESC_MAX)
    assert "error" not in out
    assert store.get(kind, "edge", None)["description"] == "x" * _DESC_MAX


def test_metadata_only_upsert_works_on_long_stored_description(store):
    _raw(store, "script", "old", "y" * 200)
    out = T.upsert_script(store, "old", language="sh")
    assert "error" not in out
    assert len(store.get("script", "old", None)["description"]) == 200


@pytest.mark.parametrize("kind", ["script", "hook"])
def test_integrity_reports_long_entity_descriptions(store, kind):
    _raw(store, kind, "verbose", "z" * 200)
    _raw(store, kind, "terse", "short")
    f = T.check_integrity(store)
    rows = f["long_entity_descriptions"]
    assert [(r["kind"], r["key"], r["length"]) for r in rows] == [(kind, "verbose", 200)]
    assert f["summary"]["long_entity_descriptions"] == 1


def test_integrity_long_entity_descriptions_empty_on_empty_store(store):
    f = T.check_integrity(store)
    assert f["long_entity_descriptions"] == []
    assert f["summary"]["long_entity_descriptions"] == 0
