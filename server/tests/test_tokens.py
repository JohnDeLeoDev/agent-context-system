'Read-side token-usage reporting.\n\nThe collector is exercised separately (it is a standalone stdlib script); what is\npinned here is the contract this module owes its callers: rollups merge across\nmachines, the local database is never silently mixed into a fleet answer, and the\namortization stays a ranking rather than a dollar decomposition.'
import json
import sqlite3
from pathlib import Path

import pytest

from agent_context import tokens


def _rollup(store, machine, month, usage_rows, tool_rows=()):
    d = Path(store.root) / "machines" / machine / "token-usage"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{month}.json").write_text(json.dumps({
        "schema": 1,
        "month": month,
        "generated_at": 0,
        "usage": {"columns": ["day", "project", "model", "effort", "sidechain",
                              "requests", "input", "cache_w5", "cache_w1h",
                              "cache_read", "output", "cost_usd",
                              "cost_input_usd"],
                  
                  
                  
                  "rows": [list(r) for r in usage_rows]},
        "tools": {"columns": ["day", "project", "tool", "calls", "result_bytes",
                              "est_tokens", "errors"],
                  "rows": [list(r) for r in tool_rows]},
    }))


def _today():
    import time
    return time.strftime("%Y-%m-%d", time.gmtime())


def _month():
    return _today()[:7]


@pytest.fixture
def two_machines(store):
    day = _today()
    _rollup(store, "machine-a", _month(),
            [(day, "/repo/alpha", "claude-opus-5", "high", 0, 10, 5, 1000, 0, 50_000, 2_000, 12.0),
             (day, "/repo/beta", "claude-sonnet-5", "high", 1, 4, 2, 400, 0, 10_000, 500, 3.0)],
            [(day, "/repo/alpha", "Read", 6, 400_000, 100_000, 0),
             (day, "/repo/alpha", "Bash:grep", 20, 40_000, 10_000, 2)])
    _rollup(store, "machine-b", _month(),
            [(day, "/repo/alpha", "claude-opus-5", "high", 0, 6, 3, 600, 0, 20_000, 900, 5.0)],
            [(day, "/repo/alpha", "Read", 3, 100_000, 25_000, 0)])
    return store


def test_no_rollups_is_a_pointer_not_an_error(store):
    r = tokens.token_report(store)
    assert r["requests"] == 0
    assert r["source"] == "none"
    assert "token-usage-collect" in r["note"]


def test_merges_every_machine(two_machines):
    r = tokens.token_report(two_machines, group_by="project")
    assert r["machines_reporting"] == 2
    assert sorted(r["machines"]) == ["machine-a", "machine-b"]
    assert r["totals"]["requests"] == 20
    assert r["totals"]["cost_usd"] == pytest.approx(20.0)
    alpha = next(g for g in r["groups"] if g["project"] == "/repo/alpha")
    assert alpha["cost_usd"] == pytest.approx(17.0)   
    assert alpha["share_pct"] == pytest.approx(85.0)


def test_per_machine_excludes_the_fleet(two_machines):
    r = tokens.token_report(two_machines, per_machine=True, machine_uuid="machine-a")
    assert r["machines_reporting"] == 1
    assert r["totals"]["cost_usd"] == pytest.approx(15.0)
    assert "this machine's only" in r["note"]


def test_sidechain_grouping_names_the_halves(two_machines):
    r = tokens.token_report(two_machines, group_by="sidechain")
    keys = {g["sidechain"] for g in r["groups"]}
    assert keys == {"main", "subagent"}


def test_cache_write_sums_both_ttls(store):
    day = _today()
    _rollup(store, "m", _month(),
            [(day, "/r", "claude-opus-5", "high", 0, 1, 0, 700, 300, 0, 10, 1.0)])
    r = tokens.token_report(store)
    assert r["totals"]["cache_write_tokens"] == 1000


def test_window_excludes_older_days(store):
    _rollup(store, "m", "2001-01",
            [("2001-01-01", "/r", "claude-opus-5", "high", 0, 99, 0, 0, 0, 0, 0, 99.0)])
    assert tokens.token_report(store, since_days=30)["requests"] == 0


def test_tools_ranked_by_context_share(two_machines):
    r = tokens.token_report(two_machines)
    read = r["tools_by_context"][0]
    assert read["tool"] == "Read"
    assert read["calls"] == 9                 
    assert read["result_tokens"] == 125_000
    assert read["context_share_pct"] == pytest.approx(92.6, abs=0.2)


def test_project_filter_narrows_tools_too(two_machines):
    r = tokens.token_report(two_machines, project="/repo/beta")
    assert r["totals"]["requests"] == 4
    assert r["tools_by_context"] == []


def test_rejects_unknown_group_by(store):
    assert "error" in tokens.token_report(store, group_by="wat")


def test_corrupt_rollup_is_skipped_not_fatal(two_machines):
    bad = Path(two_machines.root) / "machines" / "machine-a" / "token-usage" / "2026-99.json"
    bad.write_text("{not json")
    r = tokens.token_report(two_machines)
    assert r["totals"]["requests"] == 20


def test_price_strips_a_date_suffix():
    assert tokens._price("claude-haiku-4-5-20251001") == (1.0, 5.0)
    assert tokens._price("claude-opus-5") == (5.0, 25.0)
    assert tokens._price("something-new") == tokens.FALLBACK_PRICE


def test_price_is_effective_dated_like_the_collector():
    "Must agree with token-usage-collect.py's price_for(): the amortized\n    ranking weights by rate, so pricing a request from inside Sonnet 5's intro\n    window at the later rate would overstate that tool's share by 50%."
    assert tokens._price("claude-sonnet-5", "2026-08-20") == (2.0, 10.0)
    assert tokens._price("claude-sonnet-5", "2026-08-31") == (2.0, 10.0)
    assert tokens._price("claude-sonnet-5", "2026-09-01") == (3.0, 15.0)
    assert tokens._price("claude-sonnet-5") == (3.0, 15.0)      




def _local_db(path):
    con = sqlite3.connect(str(path))
    con.executescript("""
      CREATE TABLE requests (request_id TEXT PRIMARY KEY, ts TEXT, day TEXT,
        session_id TEXT, root_session TEXT, project TEXT, git_branch TEXT,
        model TEXT, effort TEXT, sidechain INT, input_tokens INT, cache_w5 INT,
        cache_w1h INT, cache_read INT, output_tokens INT, cost_usd REAL);
      CREATE TABLE tool_calls (tool_use_id TEXT PRIMARY KEY, day TEXT,
        session_id TEXT, project TEXT, model TEXT, tool TEXT, src TEXT,
        req_ordinal INT);
      CREATE TABLE tool_results (tool_use_id TEXT PRIMARY KEY, result_bytes INT,
        est_tokens INT, is_error INT);
      CREATE TABLE files (path TEXT PRIMARY KEY, inode INT, size INT, offset INT,
        req_count INT, mtime REAL);
    """)
    return con


def test_session_grouping_reads_the_local_db(store, tmp_path, monkeypatch):
    day = _today()
    _rollup(store, "m", _month(),
            [(day, "/r", "claude-opus-5", "high", 0, 2, 0, 0, 0, 0, 0, 9.0)])
    dbp = tmp_path / "tu.db"
    con = _local_db(dbp)
    con.executemany("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("r1", "", day, "s1", "sess-cheap", "/r", None, "claude-opus-5", "high", 0,
         0, 0, 0, 0, 100, 1.0),
        ("r2", "", day, "s2", "sess-pricey", "/r", None, "claude-opus-5", "high", 0,
         0, 0, 0, 0, 900, 8.0),
    ])
    con.commit()
    con.close()
    monkeypatch.setattr(tokens, "db_path", lambda: dbp)

    r = tokens.token_report(store, group_by="session")
    assert r["source"] == "local database (this machine only)"
    assert [g["session"] for g in r["groups"]] == ["sess-pri", "sess-che"]
    assert r["groups"][0]["cost_usd"] == pytest.approx(8.0)


def test_amortization_weights_the_tail_not_the_size(store, tmp_path, monkeypatch):
    'Two results of identical size: the one with more requests behind it must\n    rank higher. This is the whole reason the block exists — a raw byte count\n    cannot tell them apart.'
    day = _today()
    _rollup(store, "m", _month(),
            [(day, "/r", "claude-opus-5", "high", 0, 1, 0, 0, 0, 0, 0, 1.0)])
    dbp = tmp_path / "tu.db"
    con = _local_db(dbp)
    con.execute("INSERT INTO files VALUES ('f',1,0,0,100,0)")
    con.executemany("INSERT INTO tool_calls VALUES (?,?,?,?,?,?,?,?)", [
        ("t-early", day, "s", "/r", "claude-opus-5", "EarlyTool", "f", 1),
        ("t-late", day, "s", "/r", "claude-opus-5", "LateTool", "f", 99),
    ])
    con.executemany("INSERT INTO tool_results VALUES (?,?,?,?)", [
        ("t-early", 40_000, 10_000, 0),
        ("t-late", 40_000, 10_000, 0),
    ])
    con.commit()
    con.close()
    monkeypatch.setattr(tokens, "db_path", lambda: dbp)

    r = tokens.token_report(store)
    ranked = r["tools_by_amortized_context"]
    assert [t["tool"] for t in ranked] == ["EarlyTool", "LateTool"]
    assert ranked[0]["result_tokens"] == ranked[1]["result_tokens"]
    assert ranked[0]["context_share_pct"] > ranked[1]["context_share_pct"] * 5
    assert sum(t["context_share_pct"] for t in ranked) == pytest.approx(100.0, abs=0.2)
    assert "context_share_pct" in r["amortized_note"]


def test_attributed_usd_scales_to_input_spend_not_total(store, tmp_path, monkeypatch):
    'A tool result is context: billed as a cache-write and then cache-reads,\n    never as output tokens. Normalizing against TOTAL spend would hand every\n    tool a share of the output bill it had no part in — the whole block summed\n    to the output-inclusive total before this was fixed.'
    day = _today()
    
    _rollup(store, "m", _month(),
            [(day, "/r", "claude-opus-5", "high", 0, 1, 0, 0, 0, 0, 900, 100.0, 25.0)])
    dbp = tmp_path / "tu.db"
    con = _local_db(dbp)
    con.execute("INSERT INTO files VALUES ('f',1,0,0,10,0)")
    con.execute("INSERT INTO tool_calls VALUES ('t', ?, 's', '/r', 'claude-opus-5', 'OnlyTool', 'f', 1)", (day,))
    con.execute("INSERT INTO tool_results VALUES ('t', 40000, 10000, 0)")
    con.commit()
    con.close()
    monkeypatch.setattr(tokens, "db_path", lambda: dbp)

    rows = tokens.token_report(store, group_by="project")["tools_by_amortized_context"]
    assert len(rows) == 1
    assert rows[0]["context_share_pct"] == pytest.approx(100.0)
    
    
    assert rows[0]["attributed_usd"] == pytest.approx(25.0)


def test_attributed_usd_falls_back_when_a_machine_is_on_schema_1(store, tmp_path, monkeypatch):
    "An un-upgraded machine's rollup has no cost_input_usd. Better to fall back\n    to total spend than to report a figure that is silently far too small."
    day = _today()
    _rollup(store, "m", _month(),                       
            [(day, "/r", "claude-opus-5", "high", 0, 1, 0, 0, 0, 0, 900, 100.0)])
    dbp = tmp_path / "tu.db"
    con = _local_db(dbp)
    con.execute("INSERT INTO files VALUES ('f',1,0,0,10,0)")
    con.execute("INSERT INTO tool_calls VALUES ('t', ?, 's', '/r', 'claude-opus-5', 'OnlyTool', 'f', 1)", (day,))
    con.execute("INSERT INTO tool_results VALUES ('t', 40000, 10000, 0)")
    con.commit()
    con.close()
    monkeypatch.setattr(tokens, "db_path", lambda: dbp)

    rows = tokens.token_report(store, group_by="project")["tools_by_amortized_context"]
    assert rows[0]["attributed_usd"] == pytest.approx(100.0)


def test_missing_local_db_never_breaks_the_fleet_report(two_machines, tmp_path, monkeypatch):
    monkeypatch.setattr(tokens, "db_path", lambda: tmp_path / "absent.db")
    r = tokens.token_report(two_machines)
    assert r["totals"]["requests"] == 20
    assert "tools_by_amortized_context" not in r
