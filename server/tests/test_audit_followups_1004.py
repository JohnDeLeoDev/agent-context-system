
'test audit followups 1004.'
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_context import fstools as T
from agent_context import relay_materialize as R
from agent_context import session
from agent_context.materialize import build_materialized_map


def _ago(days):
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _spent_handoffs(store):
    rows = T.check_integrity(store)["spent_records_in_live_dirs"]
    return {r["path"] for r in rows if r["kind"] == "handoff"}




def test_spent_handoff_ages_by_status_date_after_a_metadata_edit(store):
    _write_handoff(store, "handoffs/paren.md",
                   "**Status:** consumed (done 2026-01-02), next: `handoffs/2099-01-01-x.md`", 0)
    _write_handoff(store, "handoffs/two.md",
                   "**Status:** consumed, done 2026-01-03 (landed) · consumed 2026-01-02", 0)
    assert _spent_handoffs(store) == {"handoffs/paren.md", "handoffs/two.md"}


def test_recent_status_date_keeps_a_handoff_with_an_old_updated_at(store):
    _write_handoff(store, "handoffs/young.md", f"**Status:** consumed {_ago(2)[:10]}", 30)
    assert _spent_handoffs(store) == set()


def test_status_without_a_date_falls_back_to_updated_at(store):
    _write_handoff(store, "handoffs/old.md", "**Status:** consumed", 8)
    _write_handoff(store, "handoffs/new.md", "**Status:** consumed", 6)
    assert _spent_handoffs(store) == {"handoffs/old.md"}




def test_capitals_flagged_in_the_store_root_agents_md(store):
    path = os.path.join(store.root, "AGENTS.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Rules\n\nNEVER push. Run `MUST_NOT` and the API.\n")
    rows = [r for r in T.check_integrity(store)["emphatic_capitals"] if r["key"] == "AGENTS.md"]
    assert [(r["kind"], r["words"]) for r in rows] == [("root_file", {"NEVER": 1})]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Rules\n\nNever push.\n")
    assert not [r for r in T.check_integrity(store)["emphatic_capitals"]
                if r["key"] == "AGENTS.md"]




def test_the_bundle_carries_the_store_root_agents_md(tmp_path: Path) -> None:
    (tmp_path / "global").mkdir()
    assert "root/AGENTS.md" not in build_materialized_map(str(tmp_path))
    (tmp_path / "AGENTS.md").write_text("# Agent context\n", encoding="utf-8")
    assert build_materialized_map(str(tmp_path))["root/AGENTS.md"] == "# Agent context\n"


def test_a_relay_writes_agents_md_at_the_store_root_and_reprojects(tmp_path: Path) -> None:
    result = R.apply_bundle({"root/AGENTS.md": "# Agent context\n"}, tmp_path)
    assert (tmp_path / ".agent-context" / "AGENTS.md").read_text() == "# Agent context\n"
    assert result.written == ["root/AGENTS.md"]
    assert R.needs_home_materialize(result)
    assert not R.needs_settings_sync(result)


@pytest.mark.parametrize("key", ["root/global/hooks/x.py", "root/setup.sh", "root/.git/config"])
def test_root_keys_other_than_the_named_files_refuse_the_bundle(tmp_path: Path, key: str) -> None:
    with pytest.raises(R.BundleError):
        R.apply_bundle({key: "x"}, tmp_path)
    assert not (tmp_path / ".agent-context").exists()


def test_a_pushed_agents_md_refreshes_the_bundle() -> None:
    assert R.affects_bundle(["AGENTS.md"])
    assert not R.affects_bundle(["setup.sh"])




def test_inbox_is_keyed_by_machine_id_when_the_machine_has_one(store, monkeypatch):
    monkeypatch.setattr(session, "get_chezmoi_machine_id", lambda: "fleetid")
    host = socket.gethostname()
    T.upsert_doc(store, "inbox/machines/fleetid/a.md", "x", title="A")
    T.upsert_doc(store, f"inbox/machines/{host}/b.md", "x", title="B")
    T.upsert_doc(store, "inbox/machines/Fleetid/c.md", "x", title="C")
    T.add_audit_observation(store, "routine", scope="universal", project=None, evidence="e")
    paths = {i["path"] for i in T.get_session_context(store, "/no/project")["inbox"]}
    assert paths == {"inbox/machines/fleetid/a.md", "inbox/machines/fleetid/audit-digest.md"}


def test_inbox_falls_back_to_the_hostname_without_a_machine_id(store, monkeypatch):
    monkeypatch.setattr(session, "get_chezmoi_machine_id", lambda: None)
    host = socket.gethostname()
    T.upsert_doc(store, f"inbox/machines/{host}/b.md", "x", title="B")
    paths = {i["path"] for i in T.get_session_context(store, "/no/project")["inbox"]}
    assert f"inbox/machines/{host}/b.md" in paths
