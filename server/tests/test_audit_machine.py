'test audit machine.'
import pytest

from agent_context import audit
from agent_context import fstools as T


@pytest.fixture
def here(monkeypatch):
    monkeypatch.setattr(audit, "_this_machine", lambda: "m4")
    return "m4"


def _file(store, text="a defect worth recording", **kw):
    return T.add_audit_observation(store, text, scope="universal",
                                   evidence="file.py:1 — quoted line", **kw)


def test_new_observation_is_stamped_with_this_machine(store, here):
    rec = _file(store)
    assert rec["machine"] == "m4"


def test_same_machine_reads_as_local(store, here):
    _file(store)
    row = T.list_audit_observations(store)[0]
    assert row["observed_elsewhere"] is False


def test_other_machine_is_flagged(store, here, monkeypatch):
    _file(store)
    monkeypatch.setattr(audit, "_this_machine", lambda: "laptop")
    row = T.list_audit_observations(store)[0]
    assert row["observed_elsewhere"] is True


def test_resolving_from_another_machine_warns(store, here, monkeypatch):
    oid = _file(store)["id"]
    monkeypatch.setattr(audit, "_this_machine", lambda: "laptop")

    out = T.resolve_audit_observation(store, oid, resolution_note="works for me")

    assert out["status"] == "resolved"          
    assert out["resolved_on"] == "laptop"
    assert "m4" in out["warning"] and "host-specific" in out["warning"]


def test_resolving_on_the_filing_machine_does_not_warn(store, here):
    oid = _file(store)["id"]
    out = T.resolve_audit_observation(store, oid, resolution_note="fixed")
    assert "warning" not in out


def test_bootstrap_row_carries_the_host_only_when_it_is_not_ours(store, here, monkeypatch):
    _file(store)
    o = T.list_audit_observations(store)[0]
    assert "observed_on" not in audit._audit_index(o)     

    monkeypatch.setattr(audit, "_this_machine", lambda: "laptop")
    assert audit._audit_index(o)["observed_on"] == "m4"


def test_digest_bolds_a_foreign_host(store, here, monkeypatch):
    _file(store)
    assert "| m4 |" in T.audit_digest(store, "MacBook-Pro")["body"]

    monkeypatch.setattr(audit, "_this_machine", lambda: "laptop")
    assert "| **m4** |" in T.audit_digest(store, "Laptop")["body"]


def test_a_pre_stamping_record_reads_as_unknown_not_local(store, here):
    "Records filed before build 28 have no host — rendering them plainly would say\n    'mine' about a machine nobody recorded."
    _file(store)
    o = T.list_audit_observations(store)[0]
    
    raw = {k: v for k, v in o.items()
           if k not in ("age_days", "needs_reverify", "observed_elsewhere", "machine")}
    audit._audit_write(store, raw)

    row = T.list_audit_observations(store)[0]
    assert "observed_elsewhere" not in row          
    assert "| **unknown** |" in T.audit_digest(store, "MacBook-Pro")["body"]


def test_machine_can_be_backfilled(store, here):
    oid = _file(store)["id"]
    out = T.update_audit_observation(store, oid, machine="laptop")
    assert out["machine"] == "laptop"
    assert out["observed_elsewhere"] is True
    assert "| **laptop** |" in T.audit_digest(store, "MacBook-Pro")["body"]
