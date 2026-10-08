'The janitor regenerates the root pages after an archive sweep moves a handoff.'
import shutil
import time
from pathlib import Path

from agent_context import docs, janitor

SCRIPT = Path(__file__).resolve().parents[2] / "global" / "scripts" / "entity-root-pages.py"
STALE = "# agent-context-store\n\n- [[global/docs/handoffs/h|h]]\n"


def _seed(store):
    "A store root the generator can walk, with one archived handoff and a stale\n    root page that still links the handoff's old path."
    root = Path(store.root)
    for d in ("projects", "workspaces", "machines", "global/scripts",
              "global/docs/archive/handoffs"):
        (root / d).mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, root / "global" / "scripts" / "entity-root-pages.py")
    (root / "global/docs/archive/handoffs/h.md").write_text(
        '---\ntype: "doc"\npath: "archive/handoffs/h.md"\n---\n\n# h\n')
    docs.upsert_doc(store, "agent-context-store.md", body=STALE, title="agent-context-store")


def _body(store):
    return store.get("doc", "agent-context-store.md", None)["body"]


def test_pages_are_regenerated_after_a_sweep_that_moved_a_handoff(store):
    _seed(store)
    res = janitor.refresh_root_pages(store.root, store, {"handoffs": 1, "observations": 0})
    assert res == {"written": ["agent-context-store.md"]}, res
    body = _body(store)
    assert "[[global/docs/archive/handoffs/h|h]]" in body, body
    assert "[[global/docs/handoffs/h|h]]" not in body, body


def test_an_unchanged_page_is_not_rewritten(store):
    _seed(store)
    janitor.refresh_root_pages(store.root, store, {"handoffs": 1})
    stamp = store.get("doc", "agent-context-store.md", None)["updated_at"]
    res = janitor.refresh_root_pages(store.root, store, {"handoffs": 1})
    assert res == {"written": []}, res
    assert store.get("doc", "agent-context-store.md", None)["updated_at"] == stamp


def test_no_moved_handoff_means_no_regeneration(store):
    _seed(store)
    for archived in (None, {"handoffs": 0, "observations": 3}, "failed: store-compact exit 2"):
        assert janitor.refresh_root_pages(store.root, store, archived) is None
    assert _body(store).strip() == STALE.strip()


def test_no_store_or_no_script_is_a_no_op(store, tmp_path):
    assert janitor.refresh_root_pages(store.root, None, {"handoffs": 1}) is None
    assert janitor.refresh_root_pages(str(tmp_path), store, {"handoffs": 1}) is None


def _quiet(monkeypatch):
    for name in ("sweep_worktrees", "sweep_branches"):
        monkeypatch.setattr(janitor, name, lambda root, t: {})
    monkeypatch.setattr(janitor, "sweep_litter", lambda root, t: [])
    monkeypatch.setattr(janitor, "sweep_fleet_refs", lambda root: [])
    monkeypatch.setattr(janitor, "refresh_invariant_health", lambda root: None)
    monkeypatch.setattr(janitor, "sweep_eval_gaps", lambda root, store: {"ran": True})
    monkeypatch.setattr(janitor, "watch_bootstrap_footprint", lambda root, store: {"ran": True})
    monkeypatch.setattr(janitor, "is_fleet_filer", lambda root, uuid=None, now=None: True)


def test_the_daily_sweep_hands_the_archive_result_to_the_refresh(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    monkeypatch.setattr(janitor, "sweep_store_compact", lambda root: {"handoffs": 2})
    seen = []
    monkeypatch.setattr(janitor, "refresh_root_pages",
                        lambda root, store, archived: seen.append(archived) or {"written": []})
    out = janitor.sweep(str(tmp_path), now=time.time(), store=object())
    assert seen == [{"handoffs": 2}] and out["root_pages"] == {"written": []}, out


def test_a_failed_refresh_is_logged_and_does_not_stop_the_sweep(tmp_path, monkeypatch):
    _quiet(monkeypatch)
    monkeypatch.setattr(janitor, "sweep_store_compact", lambda root: {"handoffs": 2})

    def boom(root, store, archived):
        raise RuntimeError("entity-root-pages exit 1")
    monkeypatch.setattr(janitor, "refresh_root_pages", boom)
    out = janitor.sweep(str(tmp_path), now=time.time(), store=object())
    assert out["root_pages"].startswith("failed: ") and "exit 1" in out["root_pages"]
    assert out["store_compact"] == {"handoffs": 2} and out["eval_gaps"] == {"ran": True}
