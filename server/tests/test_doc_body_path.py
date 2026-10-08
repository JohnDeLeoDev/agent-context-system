'`upsert_doc(body_path=…)` — read a doc body off disk instead of out of the conversation.'
import pytest

from agent_context import docs
from agent_context import fstools as T

BODY = "# ledger\n\nrow 1 — ünïcode ✅\nrow 2\n"


@pytest.fixture
def body_file(tmp_path):
    p = tmp_path / "LEDGER.md"
    p.write_text(BODY, encoding="utf-8")
    return p


def test_body_path_stores_the_file_contents(store, body_file):
    T.upsert_doc(store, "m/LEDGER.md", body_path=str(body_file))
    store.reload()
    
    
    assert T.get_doc(store, "m/LEDGER.md", project=None)["body"] == BODY.rstrip("\n")


def test_body_path_returns_a_receipt_not_the_body(store, body_file):
    r = T.upsert_doc(store, "m/LEDGER.md", body_path=str(body_file))
    assert "body" not in r
    assert r["bytes"] == len(BODY.encode())     
    assert r["source_file"] == str(body_file)


def test_inline_body_still_echoes(store):
    assert T.upsert_doc(store, "m/small.md", BODY)["body"] == BODY.rstrip("\n")


def test_body_and_body_path_together_are_refused(store, body_file):
    r = T.upsert_doc(store, "m/LEDGER.md", BODY, body_path=str(body_file))
    assert "not both" in r["error"]


def test_relative_path_is_refused(store):
    assert "absolute" in T.upsert_doc(store, "m/x.md", body_path="LEDGER.md")["error"]


def test_missing_file_is_refused(store, tmp_path):
    r = T.upsert_doc(store, "m/x.md", body_path=str(tmp_path / "nope.md"))
    assert "not found" in r["error"]


def test_oversize_file_is_refused(store, tmp_path, monkeypatch):
    monkeypatch.setattr(docs, "BODY_FILE_MAX_BYTES", 4)
    p = tmp_path / "big.md"
    p.write_text("far too long", encoding="utf-8")
    assert "over the" in T.upsert_doc(store, "m/x.md", body_path=str(p))["error"]


def test_non_utf8_file_is_refused(store, tmp_path):
    p = tmp_path / "binary.md"
    p.write_bytes(b"\xff\xfe\x00binary")
    assert "not UTF-8" in T.upsert_doc(store, "m/x.md", body_path=str(p))["error"]


def test_tilde_paths_are_expanded(store, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "LEDGER.md").write_text(BODY, encoding="utf-8")
    body, err = docs.read_body_file("~/LEDGER.md")
    assert err is None and body == BODY
