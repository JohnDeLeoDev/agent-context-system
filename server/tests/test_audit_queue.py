"Draining the audit queue: severity, near-duplicate detection, in-place amendment,\nand the re-verify sweep.\n\nThe queue's failure mode was structural, not careless — arrival outran resolution,\nthere was no update tool so re-filing was the documented repair, and nothing ranked\nor re-checked what was already open. These cover each of those."
from agent_context import fstools as T

OPENCODE_A = (
    "opencode 1.18.15 streams NO session/update frames during session/prompt — only a "
    "single usage notification, then the prompt result with an empty stopReason. "
    "ACPTranscript is built exclusively from agent_message_chunk / agent_thought_chunk "
    "and tool_call updates, so the conversation renders empty.")
OPENCODE_B = (
    "Root cause for no session activity in the conversation with opencode: ACPTranscript "
    "applies ONLY agent_message_chunk / agent_thought_chunk / tool_call / usage frames. "
    "When a prompt turn has no such updates the conversation renders empty because "
    "opencode streams plain text that its ACP server may never emit as a chunk.")
UNRELATED = (
    "guard-git-write.py blocks read-only git cat-file commit <sha> because it pattern "
    "matches the bare word commit anywhere in the command line rather than at the git "
    "subcommand position. Anchor the guard on the resolved subcommand instead.")


def test_severity_is_recorded_and_defaults_to_normal(store):
    a = T.add_audit_observation(store, "x" * 60, "universal", None, "ev", severity="blocker")
    b = T.add_audit_observation(store, "y" * 60, "universal", None, "ev")
    assert a["severity"] == "blocker"
    assert b["severity"] == "normal"


def test_bogus_severity_falls_back_rather_than_erroring(store):
    r = T.add_audit_observation(store, "z" * 60, "universal", None, "ev", severity="catastrophic")
    assert r["severity"] == "normal"


def test_list_is_worst_first_then_oldest(store):
    T.add_audit_observation(store, "n" * 60, "universal", None, "e", observed_date="2026-01-01")
    T.add_audit_observation(store, "b" * 60, "universal", None, "e", observed_date="2026-06-01",
                            severity="blocker")
    T.add_audit_observation(store, "h" * 60, "universal", None, "e", observed_date="2026-03-01",
                            severity="high")
    got = [o["severity"] for o in T.list_audit_observations(store, status="open")]
    assert got == ["blocker", "high", "normal"]


def test_near_duplicate_is_flagged_with_the_id_to_amend(store):
    'The real case: two descriptions of one opencode bug, filed minutes apart.'
    first = T.add_audit_observation(store, OPENCODE_A, "universal", None, "ev")
    second = T.add_audit_observation(store, OPENCODE_B, "universal", None, "ev")
    assert "possible_duplicate_of" in second
    assert second["possible_duplicate_of"][0]["id"] == first["id"]
    assert "update_audit_observation" in second["warning"]


def test_a_distinct_observation_is_not_called_a_duplicate(store):
    'The margin is thin (0.30 between real non-duplicates vs 0.38+ for the real\n    duplicate cluster), so this is the assertion that keeps the threshold honest.'
    T.add_audit_observation(store, OPENCODE_A, "universal", None, "ev")
    other = T.add_audit_observation(store, UNRELATED, "universal", None, "ev")
    assert "possible_duplicate_of" not in other


def test_duplicates_are_never_silently_merged(store):
    'Two records survive. Merging would destroy one of two genuinely distinct\n    defects whenever the similarity call is wrong.'
    T.add_audit_observation(store, OPENCODE_A, "universal", None, "ev")
    T.add_audit_observation(store, OPENCODE_B, "universal", None, "ev")
    assert len(T.list_audit_observations(store, status="open")) == 2


def test_resolved_observations_do_not_trigger_duplicate_warnings(store):
    first = T.add_audit_observation(store, OPENCODE_A, "universal", None, "ev")
    T.resolve_audit_observation(store, first["id"])
    again = T.add_audit_observation(store, OPENCODE_B, "universal", None, "ev")
    assert "possible_duplicate_of" not in again


def test_update_amends_in_place_instead_of_refiling(store):
    r = T.add_audit_observation(store, "q" * 60, "universal", None, "first repro")
    up = T.update_audit_observation(store, r["id"], note="ruled out the watchdog",
                                    evidence="second repro", recurred=True, severity="high")
    assert up["recurrences"] == 2
    assert "first repro" in up["evidence"] and "second repro" in up["evidence"]
    assert "ruled out the watchdog" in up["notes"]
    assert up["severity"] == "high"
    assert up["last_seen"]
    assert len(T.list_audit_observations(store, status="open")) == 1   


def test_update_on_a_missing_id_reports_rather_than_raises(store):
    assert "error" in T.update_audit_observation(store, 9999, note="x")


def test_stale_open_items_are_flagged_for_reverification(store):
    fresh = T.add_audit_observation(store, "f" * 60, "universal", None, "e")
    old = T.add_audit_observation(store, "o" * 60, "universal", None, "e",
                                  observed_date="2020-01-01")
    stale = T.list_audit_observations(store, status="open", needs_reverify=True)
    ids = [o["id"] for o in stale]
    assert old["id"] in ids and fresh["id"] not in ids
    assert stale[0]["age_days"] > T._REVERIFY_AFTER_DAYS


def test_resolved_items_are_never_flagged_stale(store):
    old = T.add_audit_observation(store, "o" * 60, "universal", None, "e",
                                  observed_date="2020-01-01")
    T.resolve_audit_observation(store, old["id"])
    assert T.list_audit_observations(store, status="open", needs_reverify=True) == []


def test_integrity_surfaces_the_backlog(store):
    T.add_audit_observation(store, "o" * 60, "universal", None, "e", observed_date="2020-01-01",
                            severity="blocker")
    f = T.check_integrity(store)
    assert f["summary"]["stale_open_observations"] == 1
    assert f["stale_open_observations"][0]["severity"] == "blocker"


def test_bootstrap_rows_lead_with_blockers(store):
    T.upsert_project(store, "gh:org/repo", "P")
    for i in range(12):
        T.add_audit_observation(store, f"high finding number {i} " + "w" * 40,
                                "universal", None, "e", observed_date="2026-08-01",
                                severity="high")
    T.add_audit_observation(store, "the blocking one " + "w" * 40, "universal", None, "e",
                            observed_date="2020-01-01", severity="blocker")
    T.add_audit_observation(store, "a routine one " + "w" * 40, "universal", None, "e",
                            observed_date="2026-08-20")   
    out = T._audit_bootstrap(store, "P")
    rows = out["open_audit_observations"].splitlines()
    assert rows[0].split()[1] == "blocker"         
    assert len(rows) == T._AUDIT_ROWS_MAX
    assert out["open_audit_observations_more"]["older_in_scope"] == 3   
    assert out["open_audit_observations_more"]["routine_in_scope"] == 1


def test_malformed_observation_is_refused_and_nothing_is_written(store):
    'Server-side twin of the audit-observation-guard hook: the same four refusals,\n    now in every harness (the hook only ever ran in Claude Code).'
    r = T.add_audit_observation(
        store, "x" * 60 + ' </observation><parameter name="evidence">y', "universal", None, "ev")
    assert "error" in r and "markup" in r["error"]
    r = T.add_audit_observation(store, "x" * 60, "universal", None, None)
    assert "error" in r and "evidence" in r["error"]
    r = T.add_audit_observation(store, "x" * 60, "job-search", None, "ev")
    assert "error" in r and "scope" in r["error"]
    r = T.add_audit_observation(store, "x" * 60, "project", None, "ev")
    assert "error" in r and "project" in r["error"]
    assert T.list_audit_observations(store) == []
    ok = T.add_audit_observation(store, "x" * 60, "project", "P", "ev")
    assert "error" not in ok and ok["id"] == 1













def test_integrity_flags_leaked_tool_markup_in_an_observation(store):
    good = T.add_audit_observation(store, "a clean record " + "w" * 50, "universal",
                                   None, "clean evidence")
    bad = T.add_audit_observation(store, "a record that swallowed its own call " + "w" * 40,
                                  "universal", None, "evidence here")
    
    
    p = store.root + f"/global/audit-observations/{bad['id']:04d}.json"
    import json
    rec = json.loads(open(p).read())
    rec["notes"] = "appeared mid-session rather than at start.</note>\n</invoke>"
    open(p, "w").write(json.dumps(rec))

    f = T.check_integrity(store)
    ids = [r["id"] for r in f["observations_with_markup"]]
    assert bad["id"] in ids
    assert good["id"] not in ids
    assert f["observations_with_markup"][0]["fields"] == ["notes"]
    assert f["summary"]["observations_with_markup"] == 1


def test_integrity_does_not_flag_legitimately_quoted_markup(store):
    "0141 quotes C# doc comments as real evidence. A check that flagged any closing\n    tag would be noise on a corpus that is otherwise clean, so it matches only the\n    WRITE TOOL's own parameter names."
    T.add_audit_observation(
        store,
        "descriptions must name the wire field via <c>camelCaseWireName</c> " + "w" * 30,
        "universal", None,
        "33 descriptions carry a signature; all now use <c>...</c> and <see cref> is gone")
    f = T.check_integrity(store)
    assert f["observations_with_markup"] == []








def test_summary_leads_with_status_and_severity(store):
    T.add_audit_observation(store, "q" * 400, "universal", None, "ev", severity="high")
    row = T.list_audit_observations(store, status="open")[0]
    assert row["summary"].startswith(f"#{row['id']} [OPEN high] ")


def test_summary_carries_the_resolution_before_the_prose(store):
    r = T.add_audit_observation(store, "PROSE " + "q" * 400, "universal", None, "ev")
    T.resolve_audit_observation(store, r["id"], resolution_note="fixed in abc1234")
    row = T.list_audit_observations(store, status="resolved")[0]
    assert row["summary"].startswith(f"#{r['id']} [RESOLVED normal] ")
    
    assert "RESOLUTION: fixed in abc1234" in row["summary"][:160]
    assert row["summary"].index("RESOLUTION:") < row["summary"].index("PROSE")


def test_summary_is_bounded_but_the_full_text_is_still_on_the_record(store):
    T.add_audit_observation(store, "z" * 4000, "universal", None, "ev")
    row = T.list_audit_observations(store, status="open")[0]
    assert len(row["summary"]) < 500
    assert len(row["observation"]) == 4000


def test_summary_flags_reverify_and_other_machines(store):
    T.add_audit_observation(store, "old" + "q" * 60, "universal", None, "ev",
                            observed_date="2020-01-01")
    row = T.list_audit_observations(store, status="open")[0]
    assert "NEEDS-REVERIFY" in row["summary"]
