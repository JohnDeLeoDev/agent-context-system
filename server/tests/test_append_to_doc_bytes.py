'Context graph T9: `append_to_doc` adds to the end without deleting what is there.\n\nSame defect as the patch routes, at the one site that legitimately changes the end of a\nbody: reading the index copy, whose trailing newlines the loader stripped, made an append\nsilently drop the blank lines the file already held.'
import pytest

from agent_context import fstools as T
from agent_context import usage


@pytest.fixture(autouse=True)
def _counters(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(usage, "_data", None)
    monkeypatch.setattr(usage, "_dirty", False)
    monkeypatch.setattr(usage, "_pending", {})


def _raw(store, path):
    e = store.get("doc", path)
    assert e is not None, path
    with open(e["_path"], "rb") as fh:
        return fh.read()


def test_append_keeps_the_blank_lines_already_in_the_file(store):
    T.upsert_doc(store, "guides/a.md", "text\n\n")
    before = _raw(store, "guides/a.md")

    T.append_to_doc(store, "guides/a.md", "added\n")

    
    
    assert before.endswith(b"text\n\n\n")
    assert _raw(store, "guides/a.md").endswith(b"text\n\nadded\n\n")


def test_append_still_starts_on_a_fresh_line(store):
    T.upsert_doc(store, "guides/a.md", "no trailing newline")

    T.append_to_doc(store, "guides/a.md", "added")

    assert _raw(store, "guides/a.md").endswith(b"no trailing newline\nadded\n")
