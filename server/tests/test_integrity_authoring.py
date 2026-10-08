'check_integrity: the authoring rules (context overhaul Phase 8).\n\nFive finding classes that keep the store in the shape the overhaul left it:\ncapitals for emphasis in operative text, descriptions cut off mid-sentence, spent\nrecords the janitor should have archived, oversized memories, and paragraphs\nrepeated across agent definitions.'
import json
import os
from datetime import UTC, datetime, timedelta

from agent_context import fstools as T

NEW_CLASSES = ("emphatic_capitals", "truncated_descriptions", "spent_records_in_live_dirs",
               "oversized_memories", "repeated_agent_paragraphs")


def _ago(days):
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _keys(rows, field):
    return {r[field] for r in rows}




def test_capitals_flagged_in_each_operative_kind(store):
    T.upsert_instruction(store, "Rules", "You MUST read the file first.")
    T.upsert_agent_definition(store, "worker-x", description="d", body="NEVER edit main.")
    T.upsert_skill(store, "sk", description="d", body="ALWAYS run the suite.")
    T.upsert_command(store, "cmd", body="Do NOT push.", description="d")
    T.upsert_memory(store, "loud", "feedback", "d", "This is IMPORTANT.", load_behavior="always")
    rows = T.check_integrity(store)["emphatic_capitals"]
    got = {(r["kind"], r["key"]) for r in rows}
    assert ("instruction", "Rules") in got
    assert ("agent_definition", "worker-x") in got
    assert ("skill", "sk") in got
    assert ("command", "cmd") in got
    assert ("memory", "loud") in got
    row = next(r for r in rows if r["key"] == "Rules")
    assert row["words"] == {"MUST": 1}
    assert row["scope"] == "global"
    assert row["fix"]


def test_capitals_not_flagged_for_acronyms_identifiers_code_or_lazy(store):
    T.upsert_instruction(store, "Plain", "Use the API and the LSP. Set NO_COLOR=1.\n\n"
                         "Inline `NEVER` is quoted.\n\n```\nMUST stays in a fence\n```\n")
    T.upsert_memory(store, "quiet", "feedback", "d", "NEVER here, but lazy.", load_behavior="lazy")
    T.upsert_script(store, "s", script_body="# NEVER in a script\n", description="d",
                    language="py")
    keys = _keys(T.check_integrity(store)["emphatic_capitals"], "key")
    assert "Plain" not in keys
    assert "quiet" not in keys
    assert "s" not in keys


def test_capitals_skip_archived_docs_and_memories(store):
    T.upsert_doc(store, "archive/old.md", body="NEVER mind.")
    keys = _keys(T.check_integrity(store)["emphatic_capitals"], "key")
    assert "archive/old.md" not in keys




def test_truncated_descriptions_flag_ellipsis_and_dangling_word(store):
    T.upsert_memory(store, "cut1", "reference", "Drift on fired is", "b", load_behavior="lazy")
    T.upsert_memory(store, "cut2", "reference", "The daemon restarts when…", "b",
                    load_behavior="lazy")
    T.upsert_memory(store, "cut3", "reference", "The daemon restarts when...", "b",
                    load_behavior="lazy")
    T.upsert_script(store, "cut4", script_body="print(1)\n", description="Reads the config and",
                    language="py")
    rows = T.check_integrity(store)["truncated_descriptions"]
    assert {"cut1", "cut2", "cut3", "cut4"} <= _keys(rows, "key")
    row = next(r for r in rows if r["key"] == "cut4")
    assert row["kind"] == "script"
    assert row["description"] == "Reads the config and"


def test_truncated_descriptions_leave_complete_endings(store):
    T.upsert_memory(store, "ok1", "reference", "Custom split screens must opt in", "b",
                    load_behavior="lazy")
    T.upsert_memory(store, "ok2", "reference", "The toggle stays turned on", "b",
                    load_behavior="lazy")
    T.upsert_memory(store, "ok3", "reference", "Restart the daemon.", "b", load_behavior="lazy")
    keys = _keys(T.check_integrity(store)["truncated_descriptions"], "key")
    assert not keys & {"ok1", "ok2", "ok3"}




def _write_obs(store, oid, status, resolved_days_ago, archive=False):
    d = os.path.join(store.root, "global",
                     "audit-observations-archive" if archive else "audit-observations")
    os.makedirs(d, exist_ok=True)
    rec = {"id": oid, "status": status, "observation": "x", "created_at": _ago(90),
           "observed_date": _ago(90)}
    if resolved_days_ago is not None:
        rec["resolved_date"] = _ago(resolved_days_ago)
    with open(os.path.join(d, f"{oid:04d}.json"), "w", encoding="utf-8") as fh:
        json.dump(rec, fh)


def _write_handoff(store, path, status_line, days_ago):
    T.upsert_doc(store, path, body=f"# Handoff\n\n{status_line} · **Opened:** 2026-01-01\n")
    p = os.path.join(store.root, "global", "docs", path)
    with open(p, encoding="utf-8") as fh:
        text = fh.read()
    lines = [f'updated_at: "{_ago(days_ago)}"' if ln.startswith("updated_at:") else ln
             for ln in text.split("\n")]
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    store.reload()


def test_spent_observations_flagged_past_thirty_days(store):
    _write_obs(store, 1, "resolved", 31)
    _write_obs(store, 2, "discarded", 45)
    rows = T.check_integrity(store)["spent_records_in_live_dirs"]
    obs = {r["id"] for r in rows if r["kind"] == "observation"}
    assert obs == {1, 2}
    assert all(r["fix"] for r in rows)


def test_spent_observations_not_flagged_when_young_open_or_archived(store):
    _write_obs(store, 3, "resolved", 29)
    _write_obs(store, 4, "open", None)
    _write_obs(store, 5, "resolved", 60, archive=True)
    rows = T.check_integrity(store)["spent_records_in_live_dirs"]
    assert not [r for r in rows if r["kind"] == "observation"]


def test_spent_handoffs_flagged_past_seven_days(store):
    _write_handoff(store, "handoffs/old-consumed.md", "**Status:** consumed 2026-01-01", 8)
    _write_handoff(store, "handoffs/old-stale.md", "**Status:** stale 2026-01-01 — moot", 10)
    rows = T.check_integrity(store)["spent_records_in_live_dirs"]
    paths = {r["path"] for r in rows if r["kind"] == "handoff"}
    assert paths == {"handoffs/old-consumed.md", "handoffs/old-stale.md"}


def test_spent_handoffs_not_flagged_when_young_or_open(store):
    _write_handoff(store, "handoffs/young.md", "**Status:** consumed", 6)
    _write_handoff(store, "handoffs/open.md", "**Status:** open", 30)
    rows = T.check_integrity(store)["spent_records_in_live_dirs"]
    assert not [r for r in rows if r["kind"] == "handoff"]




def test_memory_over_six_thousand_bytes_is_flagged(store):
    T.upsert_memory(store, "big", "project", "d", "x" * 6001, load_behavior="lazy")
    T.upsert_memory(store, "edge", "project", "d", "x" * 6000, load_behavior="lazy")
    rows = T.check_integrity(store)["oversized_memories"]
    assert _keys(rows, "slug") == {"big"}
    assert rows[0]["bytes"] == 6001
    assert rows[0]["limit"] == 6000


def test_memory_size_counts_utf8_bytes(store):
    T.upsert_memory(store, "wide", "project", "d", "é" * 3001, load_behavior="lazy")
    assert _keys(T.check_integrity(store)["oversized_memories"], "slug") == {"wide"}




SHARED = ("Report the failing command and its exit code before you propose any fix, so "
          "the lead can reproduce it.")


def test_paragraph_in_two_agent_definitions_is_flagged(store):
    T.upsert_agent_definition(store, "worker-a", description="d", body=f"Role A.\n\n{SHARED}")
    T.upsert_agent_definition(store, "worker-b", description="d",
                              body=f"Role B.\n\n{SHARED.replace(' so ', '  so\n')}")
    rows = T.check_integrity(store)["repeated_agent_paragraphs"]
    assert len(rows) == 1
    assert rows[0]["agents"] == ["worker-a", "worker-b"]
    assert rows[0]["paragraph"].startswith("Report the failing command")


def test_short_and_sanctioned_shared_paragraphs_are_not_flagged(store):
    pointer = ('**First call: `get_doc("worker-shared-rules.md")`** (on Claude Code, load it '
               "with ToolSearch first). It holds the rules every worker shares.")
    narration = ("**No narration between tool calls. No offers. No `Confidence:` or `Severity:` "
                 "tags.** Report the result.")
    body = f"Short shared line.\n\n{pointer}\n\n{narration}"
    T.upsert_agent_definition(store, "worker-a", description="d", body="Role A.\n\n" + body)
    T.upsert_agent_definition(store, "worker-b", description="d", body="Role B.\n\n" + body)
    assert T.check_integrity(store)["repeated_agent_paragraphs"] == []




def test_empty_store_reports_zero_for_every_new_class(store):
    f = T.check_integrity(store)
    for k in NEW_CLASSES:
        assert f[k] == []
        assert f["summary"][k] == 0


def test_existing_classes_still_present(store):
    f = T.check_integrity(store)
    for k in ("cross_scope_duplicate_slugs", "dangling_links", "long_memory_descriptions",
              "missing_load_behavior", "split_candidates", "stale_open_observations"):
        assert k in f
        assert k in f["summary"]
    assert "graph_coverage" not in f["summary"]
    assert "bootstrap_footprint" not in f["summary"]
