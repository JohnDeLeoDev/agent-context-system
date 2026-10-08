'A reachable machine that has stopped publishing is reported early, not only after\nit goes stale.'
from agent_context import fleet
from agent_context import fstools as T


def test_reachable_machine_reports_after_first_missed_publish(monkeypatch):
    now = 2_000_000
    monkeypatch.setattr(fleet, "_ssh_reachable", lambda host: host == "rp.example")
    rows = [{"machine_id": "rp", "hostname": "rp.example", "age_secs": 2 * 3600,
             "stale": False, "sleeps": False, "verdict": "healthy"}]
    fleet.add_early_reachability(rows)
    assert rows[0]["reachable"] is True
    assert any("rp: reachable but not reporting" in p for p in fleet.problems(rows, now=now))


def test_sleeping_or_unreachable_machine_has_no_early_alert(monkeypatch):
    monkeypatch.setattr(fleet, "_ssh_reachable", lambda host: False)
    rows = [{"machine_id": "pc", "hostname": "pc.example", "age_secs": 2 * 3600,
             "stale": False, "sleeps": False, "verdict": "healthy"},
            {"machine_id": "laptop", "hostname": "laptop.example", "age_secs": 2 * 3600,
             "stale": False, "sleeps": True, "verdict": "healthy"}]
    fleet.add_early_reachability(rows)
    assert fleet.problems(rows) == []


def test_session_bootstrap_includes_early_alert(store, monkeypatch):
    monkeypatch.setattr(store, "_maybe_fetch_for_guard", lambda: None)
    monkeypatch.setattr(fleet, "read_all", lambda root: [
        {"machine_id": "rp", "hostname": "rp.example", "age_secs": 2 * 3600,
         "stale": False, "sleeps": False, "verdict": "healthy"}])
    monkeypatch.setattr(fleet, "_ssh_reachable", lambda host: host == "rp.example")
    ctx = T.get_session_context(store, "/no/project")
    assert any("rp: reachable but not reporting" in problem
               for problem in ctx["fleet_health"])
