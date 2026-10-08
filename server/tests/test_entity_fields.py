'hosts, sources and verified_at: written through bulk_edit, read by check_integrity.'
import json
import os
import time

from agent_context import fstools as T
from agent_context import machine


def _mem(store, slug="m"):
    T.upsert_memory(store, slug, "reference", "desc", "body text", project=None,
                    load_behavior="lazy")


def test_fields_are_written_without_touching_the_body(store):
    _mem(store)
    out = T.bulk_edit(store, [{"kind": "memory", "key": "m", "fields": {
        "keywords": ["alpha"], "sources": ["global/scripts/x.py"],
        "verified_at": "2026-10-04", "description": "new desc"}}])
    assert out["entities_with_fields_set"] == 1 and out["replacements"] == 0
    m = T.get_memory(store, "m")
    assert m["body"] == "body text" and m["description"] == "new desc"
    assert m["keywords"] == ["alpha"] and m["verified_at"] == "2026-10-04"
    
    again = T.bulk_edit(store, [{"kind": "memory", "key": "m",
                                 "fields": {"keywords": ["alpha"]}}])
    assert again["entities_changed"] == 0


def test_fields_clear_and_refuse_bad_values(store):
    _mem(store)
    T.bulk_edit(store, [{"kind": "memory", "key": "m", "fields": {"keywords": ["alpha"]}}])
    T.bulk_edit(store, [{"kind": "memory", "key": "m", "fields": {"keywords": []}}])
    assert "keywords" not in T.get_memory(store, "m")
    for bad in ({"verified_at": "yesterday"}, {"description": "x" * 141},
                {"description": ""}, {"shade": "red"}, {"keywords": "alpha"}):
        out = T.bulk_edit(store, [{"kind": "memory", "key": "m", "fields": bad}])
        assert out["results"][0].get("error"), bad


def test_hosts_must_name_a_fleet_machine(store):
    _mem(store)
    os.makedirs(os.path.join(store.root, "machines"), exist_ok=True)
    with open(os.path.join(store.root, "machines", "abc.toml"), "w") as f:
        f.write('machine_id = "server-host"\n')
    bad = T.bulk_edit(store, [{"kind": "memory", "key": "m", "fields": {"hosts": ["mars"]}}])
    assert "unknown machine" in bad["results"][0]["error"]
    T.bulk_edit(store, [{"kind": "memory", "key": "m", "fields": {"hosts": ["server-host"]}}])
    assert T.get_memory(store, "m")["hosts"] == ["server-host"]


def test_fields_and_replacements_land_in_one_write(store):
    T.upsert_doc(store, "d.md", "old word", title="D")
    out = T.bulk_edit(store, [{"kind": "doc", "key": "d.md", "replacements": [["old", "new"]],
                               "fields": {"description": "Read when testing."}}])
    assert out["replacements"] == 1 and out["entities_changed"] == 1
    d = T.get_doc(store, "d.md")
    assert d["body"] == "new word" and d["description"] == "Read when testing."


def test_file_path_drives_the_batch_and_reports_only_failures(store, tmp_path):
    _mem(store)
    batch = tmp_path / "batch.json"
    batch.write_text(json.dumps([
        {"kind": "memory", "key": "m", "fields": {"keywords": ["alpha"]}},
        {"kind": "memory", "key": "no-such", "fields": {"keywords": ["beta"]}}]))
    out = T.bulk_edit(store, None, file_path=str(batch))
    assert out["entities"] == 2 and out["entities_with_fields_set"] == 1
    assert [r["key"] for r in out["results"]] == ["no-such"]
    assert T.bulk_edit(store, [{"kind": "memory", "key": "m"}], file_path=str(batch)).get("error")


def test_dry_run_writes_no_fields(store):
    _mem(store)
    T.bulk_edit(store, [{"kind": "memory", "key": "m", "fields": {"keywords": ["alpha"]}}],
                dry_run=True)
    assert "keywords" not in T.get_memory(store, "m")


def test_integrity_reports_overdue_verification_and_missing_sources(store, tmp_path, monkeypatch):
    monkeypatch.setattr(machine, "get_chezmoi_machine_id", lambda: "server-host")
    os.makedirs(os.path.join(store.root, "machines"), exist_ok=True)
    for name in ("server-host", "m4"):
        with open(os.path.join(store.root, "machines", name + ".toml"), "w") as f:
            f.write(f'machine_id = "{name}"\n')
    present = tmp_path / "present.conf"
    present.write_text("x")
    for slug in ("fresh", "old", "gone", "elsewhere", "never"):
        _mem(store, slug)
    today = time.strftime("%Y-%m-%d")
    T.bulk_edit(store, [
        {"kind": "memory", "key": "fresh", "fields": {
            "verified_at": today, "sources": [str(present), "run this command"]}},
        {"kind": "memory", "key": "old", "fields": {"verified_at": "2020-01-01"}},
        {"kind": "memory", "key": "gone", "fields": {
            "sources": [str(tmp_path / "missing.conf"), "global/scripts/missing.py"]}},
        {"kind": "memory", "key": "elsewhere", "fields": {
            "hosts": ["m4"], "sources": [str(tmp_path / "missing.conf")]}}])
    found = T.check_integrity(store, summary=True)
    assert [r["key"] for r in found["verification_overdue"]] == ["old"]
    assert {(r["key"], r["source"]) for r in found["missing_sources"]} == {
        ("gone", "global/scripts/missing.py"), ("gone", str(tmp_path / "missing.conf"))}
    assert found["verification"]["verified"] == 2
    assert found["verification"]["never_verified"] == 3
