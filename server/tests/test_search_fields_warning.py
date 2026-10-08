'upsert_memory and upsert_doc warn when search fields are missing (policy).'
import os

from agent_context import integrity, machine, server
from agent_context import fstools as T


def test_memory_without_keywords_is_warned():
    out = server._search_fields_warning({"slug": "m"}, "memory")
    assert "keywords" in out["warning"]
    assert "warning" not in server._search_fields_warning({"slug": "m", "keywords": ["a"]},
                                                          "memory")


def test_doc_needs_keywords_and_description():
    out = server._search_fields_warning({"path": "guide.md", "keywords": ["a"]}, "doc")
    assert "description" in out["warning"] and "keywords" not in out["warning"].split(":")[0]
    full = {"path": "guide.md", "keywords": ["a"], "description": "Read when x."}
    assert "warning" not in server._search_fields_warning(full, "doc")


def test_archive_and_handoff_docs_are_not_warned():
    for path in ("archive/x-history.md", "handoffs/2026-10-04-x.md", "inbox/machines/ls/x.md"):
        assert "warning" not in server._search_fields_warning({"path": path}, "doc")


def test_an_error_or_an_earlier_warning_is_kept():
    assert server._search_fields_warning({"error": "no"}, "memory") == {"error": "no"}
    out = server._search_fields_warning({"slug": "m", "warning": "first"}, "memory")
    assert out["warning"].startswith("first | ")


def test_unreadable_source_is_not_reported_missing(store, tmp_path, monkeypatch):
    monkeypatch.setattr(machine, "get_chezmoi_machine_id", lambda: "server-host")
    T.upsert_memory(store, "m", "reference", "desc", "body", project=None, load_behavior="lazy")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "secret.conf").write_text("x")
    T.bulk_edit(store, [{"kind": "memory", "key": "m", "fields": {
        "sources": [str(locked / "secret.conf"), str(tmp_path / "gone.conf")]}}])
    os.chmod(locked, 0)
    try:
        if os.access(str(locked / "secret.conf"), os.F_OK):
            return                      
        rows = integrity.check_integrity(store, summary=True).get("missing_sources", [])
    finally:
        os.chmod(locked, 0o700)
    assert [r["source"] for r in rows] == [str(tmp_path / "gone.conf")]
