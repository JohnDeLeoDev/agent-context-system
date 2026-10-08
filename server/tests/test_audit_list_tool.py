"The list tool's view: a window, a cap, a compact shape, and never a silent cut."
from agent_context import audit
from agent_context import fstools as T

NOW = 1_800_000_000.0        


def _add(store, text, **kw):
    return T.add_audit_observation(store, text + " " + "w" * 50, "universal", None, "e", **kw)


def test_the_shape_is_constant(store):
    _add(store, "one")
    out = audit.list_view(store, now=NOW)
    assert set(out) == {"observations", "total", "shown", "hint"}
    assert out["total"] == out["shown"] == 1 and out["hint"] is None
    assert "observation" in out["observations"][0]      


def test_since_days_counts_any_of_the_records_dates(store):
    old = _add(store, "old", observed_date="2026-01-01")
    _add(store, "older", observed_date="2026-01-02")
    T.resolve_audit_observation(store, old["id"], "done")     
    ids = {o["id"] for o in audit.list_view(store, since_days=7)["observations"]}
    assert ids == {old["id"]}, "resolved yesterday is part of yesterday"
    assert audit.list_view(store, since_days=7, now=NOW)["total"] == 0


def test_limit_slices_after_the_drain_order_sort(store):
    _add(store, "n", observed_date="2026-01-01")
    _add(store, "b", observed_date="2026-06-01", severity="blocker")
    _add(store, "h", observed_date="2026-03-01", severity="high")
    out = audit.list_view(store, limit=2, now=NOW)
    assert [o["severity"] for o in out["observations"]] == ["blocker", "high"]
    assert out["total"] == 3 and out["shown"] == 2


def test_compact_rows_carry_exactly_the_compact_keys(store):
    _add(store, "c", severity="high")
    row = audit.list_view(store, compact=True, now=NOW)["observations"][0]
    assert set(row) == set(audit._AUDIT_COMPACT_KEYS)
    assert row["summary"].startswith("#1 [OPEN high]")


def test_an_unfiltered_call_over_the_size_threshold_comes_back_compact_with_a_hint(store):
    for i in range(5):
        _add(store, f"big {i}")
    out = audit.list_view(store, now=NOW, auto_compact_bytes=500)
    assert out["hint"] and "compact" in out["hint"] and "since_days=" in out["hint"]
    assert all(set(o) == set(audit._AUDIT_COMPACT_KEYS) for o in out["observations"])
    assert out["total"] == out["shown"] == 5


def test_a_filtered_call_is_never_auto_compacted(store):
    for i in range(5):
        _add(store, f"big {i}")
    out = audit.list_view(store, status="open", now=NOW, auto_compact_bytes=500)
    assert out["hint"] is None
    assert "observation" in out["observations"][0]


def test_the_core_list_is_still_a_plain_list(store):
    _add(store, "x")
    assert isinstance(T.list_audit_observations(store, status="open"), list)
