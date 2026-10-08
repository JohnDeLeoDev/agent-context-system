'The token-usage COLLECTOR — the write side.\n\n`test_tokens.py` covers the reporting module. This covers the script that fills\nit, which is where every subtle rule actually lives: dedupe across replayed\ntranscripts, byte-offset resumption, matching a tool_result back to the\ntool_use that named it, the rate limiter, the lock, and effective-dated pricing.\n\nThat split matters because the collector was the part that shipped bugs. Both\ndefects found while building it were here and neither was visible from the\nreports — a tool-name map built after the dedupe check silently orphaned 376 MB\nof tool results, and labeling on a leading `cd` made `Bash:cd` the top tool by\na factor of two. Each was caught by eyeballing output on real data, which is not\na method that survives the next change.\n\nThe collector is a standalone stdlib script (it runs from hooks under a bare\npython3), so it is loaded by path rather than imported as a package module. It\nlives in the same repo as this suite, so the path is deterministic.'
import importlib.util
import json
import sqlite3
import time
from pathlib import Path

import pytest

_COLLECTOR = (Path(__file__).resolve().parents[2]
              / "global" / "scripts" / "token-usage-collect.py")


def _load():
    if not _COLLECTOR.exists():                     
        pytest.skip(f"collector not present at {_COLLECTOR}")
    spec = importlib.util.spec_from_file_location("token_usage_collect", _COLLECTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tuc = _load()




def _assistant(rid, *, day="2026-08-20", model="claude-opus-5", cwd="/repo",
               out=100, cache_read=0, w5=0, w1h=0, tools=(), sidechain=False):
    return {
        "type": "assistant", "requestId": rid, "sessionId": "s1",
        "timestamp": f"{day}T12:00:00.000Z", "cwd": cwd, "isSidechain": sidechain,
        "effort": "high", "gitBranch": "main",
        "message": {"model": model, "usage": {
            "input_tokens": 5, "cache_read_input_tokens": cache_read,
            "output_tokens": out,
            "cache_creation": {"ephemeral_5m_input_tokens": w5,
                               "ephemeral_1h_input_tokens": w1h}},
            "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}
                        for tid, name, inp in tools]},
    }


def _tool_result(tid, text):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "content": text}]}}


def _write(path, entries, *, append=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    'env.'
    projects = tmp_path / "projects"
    projects.mkdir()
    store = tmp_path / "store"
    (store / ".git").mkdir(parents=True)
    monkeypatch.setenv("AGENT_CONTEXT_ROOT", str(store))
    monkeypatch.setenv("TOKEN_ROLLUP_DIR", str(tmp_path / "rollup"))
    uploads = []
    monkeypatch.setattr(tuc, "upload_month",
                        lambda month, usage, tools=None: uploads.append((month, usage, tools)))
    tuc._repo_root.cache_clear()
    return {"projects": projects, "db": tmp_path / "tu.db", "tmp": tmp_path,
            "rollup": tmp_path / "rollup", "uploads": uploads}


def _run(env, *args):
    return tuc.main(["--projects", str(env["projects"]), "--db", str(env["db"]),
                     "--quiet", *args])


def _con(env):
    return sqlite3.connect(str(env["db"]))




def test_price_is_effective_dated():
    'test price is effective dated.'
    assert tuc.price_for("claude-sonnet-5", "2026-08-20") == (2.0, 10.0)
    assert tuc.price_for("claude-sonnet-5", "2026-08-31") == (2.0, 10.0)   
    assert tuc.price_for("claude-sonnet-5", "2026-09-01") == (3.0, 15.0)


def test_price_strips_dated_suffix_and_falls_back():
    assert tuc.price_for("claude-haiku-4-5-20251001") == (1.0, 5.0)
    assert tuc.price_for("claude-opus-5", "2026-08-20") == (5.0, 25.0)
    assert tuc.price_for("brand-new-model") == tuc.FALLBACK_PRICE


def test_cost_splits_input_from_output():
    'cost_input_usd must exclude output: a tool result is context, never an\n    output token, and the amortized ranking normalizes against it.'
    total, inp, w5, w1 = tuc.cost_of("claude-opus-5", "2026-08-20", {
        "input_tokens": 1_000_000, "output_tokens": 1_000_000,
        "cache_read_input_tokens": 0, "cache_creation": {}})
    assert inp == pytest.approx(5.0)
    assert total == pytest.approx(30.0)
    assert (w5, w1) == (0, 0)


def test_cache_write_tiers_are_priced_apart():
    '1h cache write is 2x base input, 5m is 1.25x — not the same number.'
    _, five, _, _ = tuc.cost_of("claude-opus-5", "2026-08-20",
                                {"cache_creation": {"ephemeral_5m_input_tokens": 1_000_000}})
    _, hour, _, _ = tuc.cost_of("claude-opus-5", "2026-08-20",
                                {"cache_creation": {"ephemeral_1h_input_tokens": 1_000_000}})
    assert five == pytest.approx(6.25)
    assert hour == pytest.approx(10.0)


def test_flat_cache_total_is_treated_as_5m():
    'Older records carry only cache_creation_input_tokens.'
    _, usd, w5, w1 = tuc.cost_of("claude-opus-5", "2026-08-20",
                                 {"cache_creation_input_tokens": 1_000_000})
    assert (w5, w1) == (1_000_000, 0)
    assert usd == pytest.approx(6.25)




def test_project_folds_worktree_into_its_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    tuc._repo_root.cache_clear()
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / ".claude" / "worktrees" / "feature").mkdir(parents=True)
    assert tuc.project_of(str(tmp_path / "repo" / ".claude" / "worktrees" / "feature")) == "~/repo"


def test_project_climbs_to_the_repo_root(tmp_path, monkeypatch):
    'A session launched in a subdirectory is not its own project — this is what\n    scattered one repo across six rows.'
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    tuc._repo_root.cache_clear()
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    deep = tmp_path / "repo" / "server" / "src" / "pkg"
    deep.mkdir(parents=True)
    assert tuc.project_of(str(deep)) == "~/repo"


def test_project_normalizes_home_so_machines_merge(tmp_path, monkeypatch):
    'The fleet merge depends on this: two Macs with different $HOME must\n    produce the same key for the same repo.'
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    tuc._repo_root.cache_clear()
    (tmp_path / "r" / ".git").mkdir(parents=True)
    assert tuc.project_of(str(tmp_path / "r")) == "~/r"
    
    assert tuc.project_of("/Users/someone-else/Developer/App") == "~/Developer/App"


def test_project_identity_is_idempotent(tmp_path, monkeypatch):
    '--recompute feeds stored values back in; a second pass must not mangle.'
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    tuc._repo_root.cache_clear()
    (tmp_path / "r" / ".git").mkdir(parents=True)
    once = tuc.project_of(str(tmp_path / "r"))
    assert tuc.project_of(once) == once == "~/r"


def test_project_without_a_repo_keeps_its_own_name(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    tuc._repo_root.cache_clear()
    (tmp_path / "loose").mkdir()
    assert tuc.project_of(str(tmp_path / "loose")) == "~/loose"
    assert tuc.project_of("") == "(unknown)"




def test_bash_label_strips_leading_cd():
    '`Bash:cd` was the top tool at 11,341 calls and said nothing.'
    assert tuc.tool_label("Bash", {"command": "cd /a/b && grep -rn x ."}) == "Bash:grep"
    assert tuc.tool_label("Bash", {"command": "cd /a\ngit status"}) == "Bash:git"
    assert tuc.tool_label("Bash", {"command": "cd ~/x && cd ~/y && python3 z.py"}) == "Bash:python3"
    assert tuc.tool_label("Bash", {"command": "cd /a/b"}) == "Bash:cd"   


def test_bash_label_skips_noise_and_env_assignments():
    assert tuc.tool_label("Bash", {"command": "sudo systemctl restart nginx"}) == "Bash:systemctl"
    assert tuc.tool_label("Bash", {"command": "FOO=1 BAR=2 make build"}) == "Bash:make"


def test_agent_and_skill_labels_carry_their_subject():
    assert tuc.tool_label("Agent", {"subagent_type": "general-purpose"}) == "Agent:general-purpose"
    assert tuc.tool_label("Agent", {}) == "Agent:default"
    assert tuc.tool_label("Skill", {"skill": "code-review"}) == "Skill:code-review"
    assert tuc.tool_label("Read", {"file_path": "/x"}) == "Read"




def test_dedupes_replayed_requests(env):
    'A resumed session replays history into a NEW transcript. On the first real\n    scan that was 53,364 of 124,886 records; counting them inflates ~75%.'
    a = _assistant("req-1")
    _write(env["projects"] / "sess-a.jsonl", [a])
    _write(env["projects"] / "sess-b.jsonl", [a, _assistant("req-2")])
    _run(env)
    with _con(env) as c:
        assert c.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 2


def test_resumes_from_byte_offset(env):
    'Appending must ingest only the new lines, not re-read the file.'
    f = env["projects"] / "s.jsonl"
    _write(f, [_assistant("r1")])
    _run(env)
    with _con(env) as c:
        first_offset = c.execute("SELECT offset FROM files").fetchone()[0]
    _write(f, [_assistant("r2")], append=True)
    _run(env)
    with _con(env) as c:
        assert c.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 2
        assert c.execute("SELECT offset FROM files").fetchone()[0] > first_offset


def test_partial_trailing_line_is_not_consumed(env):
    'A transcript can be mid-write. Half a JSON object must be left for the\n    next run, not dropped and not parsed.'
    f = env["projects"] / "s.jsonl"
    _write(f, [_assistant("r1")])
    with f.open("a") as fh:
        fh.write('{"type":"assistant","requestId":"r2","mess')   
    _run(env)
    with _con(env) as c:
        assert c.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    with f.open("a") as fh:                                       
        fh.write("\n")
        fh.write(json.dumps(_assistant("r2")) + "\n")
    _run(env)
    with _con(env) as c:
        assert c.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 2


def test_tool_result_matches_across_an_incremental_boundary(env):
    'The tool_use and its result land in different runs. An in-memory per-file\n    map cannot survive that, which is why tool_calls is a table.'
    f = env["projects"] / "s.jsonl"
    _write(f, [_assistant("r1", tools=[("t1", "Read", {"file_path": "/x"})])])
    _run(env)
    _write(f, [_tool_result("t1", "x" * 400)], append=True)
    _run(env)
    with _con(env) as c:
        row = c.execute(
            "SELECT c.tool, r.result_bytes FROM tool_calls c "
            "JOIN tool_results r USING(tool_use_id)").fetchone()
    assert row == ("Read", 400)


def test_tool_name_survives_a_deduped_request(env):
    "The ONLY copy of a tool's name can sit on a replayed request that dedupe\n    discards. Registering names before the dedupe decision is what fixes it."
    a = _assistant("req-1", tools=[("t9", "Read", {"file_path": "/x"})])
    _write(env["projects"] / "a.jsonl", [a])
    _run(env)
    
    _write(env["projects"] / "b.jsonl", [a, _tool_result("t9", "y" * 80)])
    _run(env)
    with _con(env) as c:
        assert c.execute("SELECT tool FROM tool_calls WHERE tool_use_id='t9'").fetchone()[0] == "Read"
        assert c.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


def test_subagent_transcript_attributes_to_its_parent_session(env):
    sub = env["projects"] / "sess-1" / "subagents" / "agent-x.jsonl"
    _write(sub, [_assistant("r1", sidechain=True)])
    _run(env)
    with _con(env) as c:
        root, side = c.execute("SELECT root_session, sidechain FROM requests").fetchone()
    assert root == "sess-1"
    assert side == 1


def test_rewritten_file_is_reread_without_duplicating(env):
    'Truncation/rotation restarts from byte 0; dedupe keeps it idempotent.'
    f = env["projects"] / "s.jsonl"
    _write(f, [_assistant("r1"), _assistant("r2")])
    _run(env)
    _write(f, [_assistant("r1"), _assistant("r2"), _assistant("r3")])   
    _run(env)
    with _con(env) as c:
        assert c.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 3


def test_unpriced_model_is_reported_not_silently_guessed(env, capsys):
    _write(env["projects"] / "s.jsonl", [_assistant("r1", model="claude-brand-new-9")])
    tuc.main(["--projects", str(env["projects"]), "--db", str(env["db"])])
    assert "claude-brand-new-9" in capsys.readouterr().out




def test_min_interval_skips_a_recent_run(env):
    _write(env["projects"] / "s.jsonl", [_assistant("r1")])
    _run(env)
    _write(env["projects"] / "s.jsonl", [_assistant("r1"), _assistant("r2")])
    _run(env, "--min-interval", "300")                 
    with _con(env) as c:
        assert c.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    _run(env, "--min-interval", "0")                   
    with _con(env) as c:
        assert c.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 2


def test_stamp_advances_even_when_nothing_was_ingested(env):
    "Keying the guard off the DB's mtime would fail exactly here: an idle scan\n    writes nothing, so the guard would never trip when there is nothing to do."
    _write(env["projects"] / "s.jsonl", [_assistant("r1")])
    _run(env)
    stamp = tuc.stamp_path(env["db"])
    old = stamp.stat().st_mtime
    time.sleep(0.02)
    _run(env)                                          
    assert stamp.stat().st_mtime > old


def test_lock_makes_a_concurrent_run_a_no_op(env):
    lock = env["db"].with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("999999")
    _write(env["projects"] / "s.jsonl", [_assistant("r1")])
    _run(env)
    assert not env["db"].exists() or _con(env).execute(
        "SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    lock.unlink()


def test_stale_lock_is_stolen_not_respected(env, monkeypatch):
    'A collector killed mid-run must never wedge collection permanently.'
    lock = env["db"].with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("999999")
    monkeypatch.setattr(tuc, "_STALE_LOCK_SECS", 0.0)
    _write(env["projects"] / "s.jsonl", [_assistant("r1")])
    _run(env)
    with _con(env) as c:
        assert c.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1




def _month_file(env):
    'The month rollup. pathlib\'s glob("*.json") also matches the upload cache,\n    `.uploaded.json`, which the collector writes beside the month files.'
    return next(p for p in sorted(env["rollup"].glob("*.json")) if not p.name.startswith("."))


def test_rollup_folds_the_long_tail_without_losing_totals(env):
    'Rows under the threshold become "(other)" — the ranking keeps its shape\n    and the totals stay exact.'
    entries = [_assistant("r1", tools=[("t1", "Read", {}), ("t2", "Read", {}),
                                       ("t3", "Read", {}), ("t4", "Rare", {})])]
    entries += [_tool_result(t, "z" * 40) for t in ("t1", "t2", "t3", "t4")]
    _write(env["projects"] / "s.jsonl", entries)
    _run(env)
    payload = json.loads(_month_file(env).read_text())
    assert env["uploads"] and env["uploads"][0][2] == payload["tools"]   
    tools = {r[payload["tools"]["columns"].index("tool")]: r
             for r in payload["tools"]["rows"]}
    assert "Read" in tools and "Rare" not in tools and "(other)" in tools
    calls = payload["tools"]["columns"].index("calls")
    assert sum(r[calls] for r in payload["tools"]["rows"]) == 4


def test_rollup_carries_the_input_side_cost(env):
    _write(env["projects"] / "s.jsonl", [_assistant("r1")])
    _run(env)
    payload = json.loads(_month_file(env).read_text())
    assert payload["schema"] >= 2
    cols = payload["usage"]["columns"]
    assert "cost_input_usd" in cols
    row = payload["usage"]["rows"][0]
    assert row[cols.index("cost_input_usd")] < row[cols.index("cost_usd")]


def test_identical_rollup_is_not_rewritten(env):
    'An idle machine must never rewrite, or upload again, an unchanged month.'
    _write(env["projects"] / "s.jsonl", [_assistant("r1")])
    _run(env)
    f = _month_file(env)
    before = f.stat().st_mtime_ns
    time.sleep(0.02)
    _run(env, "--force-rollup")
    assert f.stat().st_mtime_ns == before
    assert len(env["uploads"]) == 1
    assert not list(Path(env["tmp"] / "store").rglob("token-usage/*.json"))   




def test_recompute_reprices_history_after_a_rate_change(env, monkeypatch):
    'Rows are priced at ingest, so a corrected rate would otherwise apply only\n    to transcripts not yet swept — leaving the historical majority wrong.'
    _write(env["projects"] / "s.jsonl",
           [_assistant("r1", model="claude-sonnet-5", day="2026-08-20", out=1_000_000)])
    _run(env)
    with _con(env) as c:
        was = c.execute("SELECT cost_usd FROM requests").fetchone()[0]

    
    
    
    old_in, old_out = tuc.price_for("claude-sonnet-5", "2026-08-20")
    new_in, new_out = 30.0, 150.0
    assert (new_in / old_in) == (new_out / old_out)      
    monkeypatch.setitem(tuc.PRICES, "claude-sonnet-5", ((None, (new_in, new_out)),))

    _run(env, "--recompute")
    with _con(env) as c:
        now = c.execute("SELECT cost_usd FROM requests").fetchone()[0]
    assert now == pytest.approx(was * (new_out / old_out), rel=1e-6)


def test_recompute_is_idempotent(env):
    _write(env["projects"] / "s.jsonl", [_assistant("r1"), _assistant("r2")])
    _run(env, "--recompute")
    with _con(env) as c:
        first = c.execute("SELECT SUM(cost_usd), project FROM requests").fetchone()
    _run(env, "--recompute")
    with _con(env) as c:
        assert c.execute("SELECT SUM(cost_usd), project FROM requests").fetchone() == first


def test_stale_derivation_recomputes_without_being_asked(env, monkeypatch):
    'The fleet-wide half of a correction. Five other machines will never have\n    `--recompute` run by hand, so a bumped DERIVATION_VERSION has to re-derive on\n    the next ordinary collector run or those machines keep feeding the merge\n    numbers computed under the old rule.'
    _write(env["projects"] / "s.jsonl",
           [_assistant("r1", model="claude-sonnet-5", day="2026-08-20", out=1_000_000)])
    _run(env)
    with _con(env) as c:
        was = c.execute("SELECT cost_usd FROM requests").fetchone()[0]

    monkeypatch.setitem(tuc.PRICES, "claude-sonnet-5", ((None, (20.0, 100.0)),))
    monkeypatch.setattr(tuc, "DERIVATION_VERSION", tuc.DERIVATION_VERSION + 1)

    _run(env)                                    
    with _con(env) as c:
        now = c.execute("SELECT cost_usd FROM requests").fetchone()[0]
        marker = c.execute("SELECT v FROM meta WHERE k='derivation_version'").fetchone()[0]
    assert now == pytest.approx(was * 10, rel=1e-6)
    assert marker == str(tuc.DERIVATION_VERSION)


def test_current_derivation_does_not_recompute_every_run(env):
    'The flip side: once the marker matches, an ordinary run must not re-derive\n    72k rows on every Stop hook.'
    _write(env["projects"] / "s.jsonl", [_assistant("r1")])
    _run(env)                                    
    calls = []
    real = tuc.recompute
    tuc.recompute = lambda con: (calls.append(1), real(con))[1]
    try:
        _run(env)
    finally:
        tuc.recompute = real
    assert calls == []


def test_recompute_backfills_a_schema_1_database(env):
    'The ALTER path: an existing database predates cost_input_usd and holds the\n    only surviving copy of swept transcripts, so it is migrated, not rebuilt.'
    _write(env["projects"] / "s.jsonl", [_assistant("r1")])
    _run(env)
    with _con(env) as c:                                  
        c.execute("UPDATE requests SET cost_input_usd = 0")
        c.commit()
    _run(env, "--recompute")
    with _con(env) as c:
        assert c.execute("SELECT cost_input_usd FROM requests").fetchone()[0] > 0
