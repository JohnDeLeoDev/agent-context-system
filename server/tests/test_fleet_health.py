'fleet.py — the question no per-machine health signal could answer.\n\nThe store is already replicated everywhere, so it carries the answer itself: each\ndaemon publishes its own status, and a machine that goes quiet is detected BY the\nsilence. These tests pin the two conditions apart, because their remedies are\nopposite — a quiet machine wants a restart, a stale-code machine must NOT be restarted\n(that cold-starts onto the new code and hides the gate failure that will strand the\nnext build too).'
import json
import time

import pytest

from agent_context import fleet


@pytest.fixture
def store_root(tmp_path):
    (tmp_path / "machines").mkdir()
    return tmp_path


def _put(root, uuid, *, machine_id, verdict="healthy", code_current=True,
         build="46", age_secs=0, stale_code_secs=None, now=None,
         code_defer_reason=None, adoption=None, sync_reason=None):
    t = (time.time() if now is None else now) - age_secs
    p = root / "machines" / uuid / "daemon-status.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    since = None
    if code_current is False:
        
        
        back = 3600 if stale_code_secs is None else stale_code_secs
        since = int((time.time() if now is None else now) - back)
    p.write_text(json.dumps({
        "machine_id": machine_id, "hostname": machine_id, "build": build,
        "code_version": 1.0, "code_current": code_current,
        "code_stale_since": since, "code_defer_reason": code_defer_reason,
        "verdict": verdict,
        "sync_reason": sync_reason,
        "adoption": adoption,
        "updated_at": int(t),
    }, separators=(",", ":"), sort_keys=True))


def test_a_machine_reporting_its_own_sync_broken_is_a_problem(store_root):
    'test a machine reporting its own sync broken is a problem.'
    _put(store_root, "u1", machine_id="laptop", verdict="integration-failing",
         sync_reason="publishing HELD — commit a8941742 is unsigned")
    _put(store_root, "u2", machine_id="m4")
    h = fleet.health(store_root)
    assert h["converged"] is False
    assert len(h["problems"]) == 1
    line = h["problems"][0]
    assert line.startswith("laptop:")
    assert "integration-failing" in line
    assert "unsigned" in line, "the reason travels with the verdict"


def test_a_verdict_without_a_reason_says_so(store_root):
    'test a verdict without a reason says so.'
    _put(store_root, "u1", machine_id="rp", verdict="loop-dead")
    line = fleet.health(store_root)["problems"][0]
    assert "loop-dead" in line and "has not said why" in line
    assert "too old" not in line


def test_a_row_predating_the_reason_field_is_called_old(store_root):
    "No key at all is a build from before sync_reason existed, and saying so is\n    the one case where 'too old' is evidence rather than a guess."
    p = store_root / "machines" / "u1" / "daemon-status.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"machine_id": "rp", "hostname": "rp", "build": "40",
                             "code_version": 1.0, "code_current": True,
                             "verdict": "loop-dead", "updated_at": int(time.time())}))
    line = fleet.health(store_root)["problems"][0]
    assert "loop-dead" in line and "too old" in line


def test_starting_and_healthy_verdicts_are_not_problems(store_root):
    _put(store_root, "u1", machine_id="laptop", verdict="starting")
    _put(store_root, "u2", machine_id="m4", verdict="healthy")
    assert fleet.health(store_root)["problems"] == []


def test_publish_carries_the_reason_only_while_unhealthy(store_root):
    'Idle rows must stay byte-identical (the hourly-bucket discipline), so the\n    reason is present exactly when the verdict is not healthy.'
    fleet.publish(store_root, "u9", {"verdict": "healthy", "code_current": True,
                                      "last_sync_error": "stale text from earlier"},
                  machine_id="x", now=1_700_000_000)
    row = json.loads((store_root / "machines" / "u9" / "daemon-status.json").read_text())
    assert row["sync_reason"] is None
    fleet.publish(store_root, "u9", {"verdict": "integration-failing", "code_current": True,
                                      "last_sync_error": "merge conflicted"},
                  machine_id="x", now=1_700_000_000)
    row = json.loads((store_root / "machines" / "u9" / "daemon-status.json").read_text())
    assert row["sync_reason"] == "merge conflicted"


def _adopted(projected_age_secs=0, stale_daemons=None, commit="abc1234", now=None):
    'An adoption block as a healthy machine publishes one.'
    t = time.time() if now is None else now
    return {"projected_at": int(t - projected_age_secs),
            "projected_commit": commit, "stale_daemons": stale_daemons}


def test_a_converged_fleet_reports_no_problems(store_root):
    _put(store_root, "u1", machine_id="laptop")
    _put(store_root, "u2", machine_id="m4")
    h = fleet.health(store_root)
    assert h["converged"] is True
    assert h["problems"] == []
    assert h["reporting"] == 2


def test_a_machine_that_stops_reporting_is_the_alarm(store_root):
    'The s1/s2 case. A dead sync loop cannot raise its own alarm — but it also\n    stops updating this file, so the absence of news IS the news.'
    _put(store_root, "u1", machine_id="laptop")
    _put(store_root, "u2", machine_id="mirror-a", age_secs=20 * 3600)
    h = fleet.health(store_root)
    assert h["converged"] is False
    assert len(h["problems"]) == 1
    assert "mirror-a" in h["problems"][0]
    assert "not reporting" in h["problems"][0]
    assert "restart" in h["problems"][0]


def test_silence_does_not_prescribe_a_restart_it_names_all_three_causes(store_root):
    'Silence has three causes and this line must not pick one for you.'
    _put(store_root, "u1", machine_id="mirror-b", age_secs=7 * 3600)
    line = fleet.health(store_root)["problems"][0]
    assert "conflict" in line                       
    assert "last_sync_error" in line                
    assert "restart its daemon through the supervisor" not in line


def test_stale_code_with_no_reason_does_not_assert_a_cause(store_root):
    'A machine pinned on old code that has NOT said why (an older daemon, say).'
    _put(store_root, "u1", machine_id="server-host", code_current=False, build="43")
    h = fleet.health(store_root)
    assert h["converged"] is False
    line = h["problems"][0]
    assert "server-host" in line and "43" in line
    assert "has not said why" in line
    assert "Restarting masks a gate failure" in line
    
    assert "so its deploy gate is failing" not in line


def test_stale_code_repeats_the_machines_own_reason_verbatim(store_root):
    'When the machine published a reason, the line reports it instead of guessing.'
    _put(store_root, "u1", machine_id="m4", code_current=False, build="69",
         code_defer_reason="server/ has uncommitted edits, so the gate has NOT run — "
                           "release or revert them (policy)")
    line = fleet.health(store_root)["problems"][0]
    assert "m4" in line and "69" in line
    assert "That machine reports:" in line
    assert "uncommitted edits" in line and "the gate has NOT run" in line
    
    assert "has not said why" not in line


def test_a_quiet_machine_is_reported_as_quiet_not_as_stale_code(store_root):
    'Precedence matters. A machine that has not reported in a day may ALSO have\n    code_current False in its last message — but the actionable fact is that nobody\n    has heard from it, and its last known code state is by definition out of date.'
    _put(store_root, "u1", machine_id="mirror-b", code_current=False, age_secs=30 * 3600)
    line = fleet.health(store_root)["problems"][0]
    assert "not reporting" in line
    assert "gate" not in line


def test_the_stale_window_tolerates_a_reboot_or_a_sleeping_laptop(store_root, monkeypatch):
    'Two hours quiet is a long gate run, a reboot, or a closed lid — not an\n    incident. Paging for those is how an alarm gets ignored.'
    monkeypatch.setattr(fleet, "_ssh_reachable", lambda _: False)
    _put(store_root, "u1", machine_id="laptop", age_secs=2 * 3600)
    assert fleet.health(store_root)["problems"] == []
    _put(store_root, "u1", machine_id="laptop", age_secs=4 * 3600)
    assert fleet.health(store_root)["problems"] != []


def test_publish_is_hourly_bucketed_so_an_idle_machine_stops_committing(store_root):
    "The churn rule. Telemetry written once per turn was 83% of this repo's\n    history, and every commit is an integration on seven machines. An unchanging\n    machine must rewrite this file at most once an hour."
    h = {"code_version": 1.0, "code_current": True, "verdict": "healthy"}
    t0 = 1_800_000_000.0
    assert fleet.publish(store_root, "u1", h, machine_id="laptop", now=t0) is True
    
    for offset in (1, 60, 900, 3599):
        assert fleet.publish(store_root, "u1", h, machine_id="laptop",
                             now=t0 + offset) is False
    
    assert fleet.publish(store_root, "u1", h, machine_id="laptop",
                         now=t0 + 3600) is True


def test_a_changed_verdict_publishes_immediately(store_root):
    'A state change is worth a commit at once — the bucket exists to silence\n    unchanging machines, not to delay bad news by up to an hour.'
    t0 = 1_800_000_000.0
    ok = {"code_version": 1.0, "code_current": True, "verdict": "healthy"}
    bad = {"code_version": 1.0, "code_current": False, "verdict": "stale-code"}
    assert fleet.publish(store_root, "u1", ok, machine_id="laptop", now=t0) is True
    assert fleet.publish(store_root, "u1", bad, machine_id="laptop",
                         now=t0 + 30) is True


def test_a_release_still_landing_is_not_reported_as_a_gate_failure(store_root):
    'Every machine is briefly behind after a release — it has to fetch, gate and\n    re-exec. This view reported exactly that about the laptop minutes after build 49\n    ("its deploy gate is failing") while the laptop was adopting normally and did so\n    on the next cycle. An alarm that fires on every healthy release is one nobody\n    reads when it finally means something.'
    _put(store_root, "u1", machine_id="laptop", code_current=False,
         stale_code_secs=5 * 60)                      
    assert fleet.health(store_root)["problems"] == []


def test_code_stale_past_the_grace_window_is_reported(store_root):
    _put(store_root, "u1", machine_id="laptop", code_current=False,
         stale_code_secs=90 * 60, build="43")
    line = fleet.health(store_root)["problems"][0]
    assert "laptop" in line and "43" in line and "gate" in line
    assert "90m" in line


def test_publish_remembers_when_the_machine_first_fell_behind(store_root):
    'The grace window needs a start time, and it must survive later publishes —\n    otherwise every publish resets the clock and the window never expires.'
    t0 = 1_800_000_000.0
    behind = {"code_version": 2.0, "code_current": False, "verdict": "stale-code"}
    fleet.publish(store_root, "u1", behind, machine_id="laptop", now=t0)
    first = json.loads((store_root / "machines" / "u1" / "daemon-status.json").read_text())
    assert first["code_stale_since"] == int(t0)
    
    fleet.publish(store_root, "u1", behind, machine_id="laptop", now=t0 + 3600)
    later = json.loads((store_root / "machines" / "u1" / "daemon-status.json").read_text())
    assert later["code_stale_since"] == int(t0)


def test_catching_up_clears_the_stale_clock(store_root):
    'So the NEXT time it falls behind it gets a fresh grace window rather than\n    being judged against an old one.'
    t0 = 1_800_000_000.0
    fleet.publish(store_root, "u1", {"code_version": 2.0, "code_current": False,
                                     "verdict": "stale-code"},
                  machine_id="laptop", now=t0)
    fleet.publish(store_root, "u1", {"code_version": 2.0, "code_current": True,
                                     "verdict": "healthy"},
                  machine_id="laptop", now=t0 + 60)
    row = json.loads((store_root / "machines" / "u1" / "daemon-status.json").read_text())
    assert row["code_stale_since"] is None


def test_a_machines_dir_with_junk_in_it_does_not_break_the_read(store_root):
    'machines/ also holds <uuid>.toml rows, usage.json and token-usage/. The\n    reader must ignore everything it does not understand rather than raise inside a\n    session bootstrap.'
    _put(store_root, "u1", machine_id="laptop")
    (store_root / "machines" / "stray.toml").write_text("not a dir\n")
    (store_root / "machines" / "u2").mkdir()
    (store_root / "machines" / "u2" / "usage.json").write_text("{}")
    (store_root / "machines" / "u3").mkdir()
    (store_root / "machines" / "u3" / "daemon-status.json").write_text("{ broken")
    h = fleet.health(store_root)
    assert h["reporting"] == 1
    assert h["converged"] is True












def test_a_machine_that_projected_recently_is_not_a_problem(store_root):
    'The common case must stay silent. A line that appears on every healthy\n    machine is one people learn to skip, which costs the real finding later.'
    _put(store_root, "u1", machine_id="laptop",
         adoption=_adopted(projected_age_secs=3600))
    assert fleet.health(store_root)["problems"] == []


def test_a_day_without_projecting_is_still_normal(store_root):
    "Projection runs only from a SessionStart hook, so on a headless node\n    'whenever someone last worked there' IS the expected cadence. A day quiet is not\n    a fault, and paging for it would make this report worthless."
    _put(store_root, "u1", machine_id="server-host",
         adoption=_adopted(projected_age_secs=20 * 3600))
    assert fleet.health(store_root)["problems"] == []


def test_a_long_unprojected_machine_is_reported_with_its_remedy(store_root):
    'test a long unprojected machine is reported with its remedy.'
    _put(store_root, "u1", machine_id="rp",
         adoption=_adopted(projected_age_secs=5 * 86400, commit="deadbee"))
    h = fleet.health(store_root)
    line = h["problems"][0]
    assert "rp" in line and "5d old" in line
    assert "deadbee" in line                        
    assert "home-materialize.py --force" in line    
    assert h["converged"] is False


def test_stale_daemons_are_reported_by_key(store_root):
    'test stale daemons are reported by key.'
    _put(store_root, "u1", machine_id="m4",
         adoption=_adopted(stale_daemons=["kotlin-lsp", "csharp-ls"]))
    line = fleet.health(store_root)["problems"][0]
    assert "m4" in line and "2 language-server daemon(s)" in line
    assert "csharp-ls, kotlin-lsp" in line          
    assert "lspd.py --upgrade" in line


def test_an_empty_daemon_list_is_all_clear_and_none_is_silent(store_root):
    "[] means 'asked, all current'. None means 'could not ask' -- a daemon too old\n    to probe, or a host with no lspd. Neither is a finding, but they must not be\n    collapsed: reporting an unknown as all-clear is the failure this exists to end,\n    so None stays None all the way to the reader."
    _put(store_root, "u1", machine_id="laptop", adoption=_adopted(stale_daemons=[]))
    _put(store_root, "u2", machine_id="mirror-a", adoption=_adopted(stale_daemons=None))
    h = fleet.health(store_root)
    assert h["problems"] == []
    rows = {r["machine_id"]: r for r in h["machines"]}
    assert rows["laptop"]["adoption"]["stale_daemons"] == []
    assert rows["mirror-a"]["adoption"]["stale_daemons"] is None


def test_a_daemon_too_old_to_publish_adoption_reads_as_unknown_not_healthy(store_root):
    'Every machine runs a daemon predating this field for one release cycle. It\n    must not be reported as a fault (it is adopting normally) and must not be counted\n    as proof of health either.'
    _put(store_root, "u1", machine_id="pc", adoption=None)
    assert fleet.health(store_root)["problems"] == []


def test_adoption_is_reported_alongside_a_healthy_verdict(store_root):
    'test adoption is reported alongside a healthy verdict.'
    _put(store_root, "u1", machine_id="m4", code_current=True, verdict="healthy",
         adoption=_adopted(projected_age_secs=6 * 86400,
                           stale_daemons=["kotlin-lsp"]))
    probs = fleet.health(store_root)["problems"]
    assert len(probs) == 2
    assert any("harness projection" in p for p in probs)
    assert any("language-server daemon" in p for p in probs)


def test_a_silent_machine_is_not_also_nagged_about_adoption(store_root):
    'test a silent machine is not also nagged about adoption.'
    _put(store_root, "u1", machine_id="mirror-b", age_secs=20 * 3600,
         adoption=_adopted(projected_age_secs=30 * 86400,
                           stale_daemons=["kotlin-lsp"]))
    probs = fleet.health(store_root)["problems"]
    assert len(probs) == 1
    assert "not reporting" in probs[0]


def test_publishing_adoption_does_not_defeat_the_hourly_bucket(store_root):
    'THE CHURN TRAP THIS FIELD ALMOST INTRODUCED. The probe computes an AGE, and\n    publishing an age would rewrite the status file on every cycle -- seven machines\n    committing every five minutes, the exact churn the bucket exists to prevent. Only\n    absolute facts are published; the reader subtracts.'
    h = {"code_version": 1.0, "code_current": True, "verdict": "healthy"}
    t0 = 1_800_000_000.0
    a = {"projection": {"at": 1_799_999_000, "commit": "abc1234", "age_secs": 1000},
         "stale_daemons": []}
    assert fleet.publish(store_root, "u1", h, machine_id="laptop", now=t0,
                         adoption=a) is True
    
    for offset in (60, 900, 3599):
        a2 = {"projection": {"at": 1_799_999_000, "commit": "abc1234",
                             "age_secs": 1000 + offset},
              "stale_daemons": []}
        assert fleet.publish(store_root, "u1", h, machine_id="laptop",
                             now=t0 + offset, adoption=a2) is False


def test_a_moving_projection_stamp_does_not_defeat_the_hourly_bucket(store_root):
    'test a moving projection stamp does not defeat the hourly bucket.'
    h = {"code_version": 1.0, "code_current": True, "verdict": "healthy"}
    t0 = 1_800_000_000.0
    stamp = 1_799_996_400                      
    a = {"projection": {"at": stamp, "commit": "abc1234"}, "stale_daemons": []}
    assert fleet.publish(store_root, "u1", h, machine_id="laptop", now=t0,
                         adoption=a) is True
    for offset, commit in ((60, "bcd2345"), (900, "cde3456"), (3599, "def4567")):
        moved = {"projection": {"at": stamp + offset, "commit": commit},
                 "stale_daemons": []}
        assert fleet.publish(store_root, "u1", h, machine_id="laptop",
                             now=t0 + offset, adoption=moved) is False
    row = json.loads(fleet.status_path(store_root, "u1").read_text())
    assert row["adoption"]["projected_at"] == stamp
    assert row["adoption"]["projected_commit"] == "abc1234"   


def test_the_next_bucket_carries_the_newest_projection_commit(store_root):
    'Masked is not dropped: the commit still reaches the file, on the write the\n    bucket earns, so _adoption_problems can still name what was last projected.'
    h = {"code_version": 1.0, "code_current": True, "verdict": "healthy"}
    t0 = 1_800_000_000.0
    fleet.publish(store_root, "u1", h, machine_id="laptop", now=t0,
                  adoption={"projection": {"at": 1_799_999_000, "commit": "abc1234"},
                            "stale_daemons": []})
    assert fleet.publish(store_root, "u1", h, machine_id="laptop", now=t0 + 3600,
                         adoption={"projection": {"at": 1_800_003_000, "commit": "fed9876"},
                                   "stale_daemons": []}) is True
    row = json.loads(fleet.status_path(store_root, "u1").read_text())
    assert row["adoption"]["projected_commit"] == "fed9876"
    assert row["adoption"]["projected_at"] == 1_800_003_000 // 3600 * 3600


def test_a_projection_five_days_old_is_still_reported_after_bucketing(store_root):
    "Bucketing loses under an hour; the reader's threshold is two days."
    now = 1_800_000_000.0
    fleet.publish(store_root, "u1", {"code_version": 1.0, "code_current": True,
                                     "verdict": "healthy"},
                  machine_id="rp", now=now,
                  adoption={"projection": {"at": now - 5 * 86400, "commit": "deadbee"},
                            "stale_daemons": []})
    line = fleet.health(store_root, now=now)["problems"][0]
    assert "rp" in line and "5d old" in line and "deadbee" in line


def test_a_newly_stale_daemon_publishes_immediately(store_root):
    'A state change is news. The bucket silences unchanging machines; it must not\n    delay a daemon falling behind by up to an hour.'
    h = {"code_version": 1.0, "code_current": True, "verdict": "healthy"}
    t0 = 1_800_000_000.0
    clean = {"projection": {"at": 1_799_999_000, "commit": "abc1234"},
             "stale_daemons": []}
    dirty = {"projection": {"at": 1_799_999_000, "commit": "abc1234"},
             "stale_daemons": ["kotlin-lsp"]}
    assert fleet.publish(store_root, "u1", h, machine_id="laptop", now=t0,
                         adoption=clean) is True
    assert fleet.publish(store_root, "u1", h, machine_id="laptop", now=t0 + 30,
                         adoption=dirty) is True


def test_read_all_on_a_store_with_no_machines_dir_is_empty_not_an_error(tmp_path):
    assert fleet.read_all(tmp_path) == []
    assert fleet.health(tmp_path)["converged"] is True


def test_a_quiet_machine_asks_whether_it_is_up_before_diagnosing(store_root):
    'test a quiet machine asks whether it is up before diagnosing.'
    _put(store_root, "u1", machine_id="pc", age_secs=13 * 3600)
    line = fleet.health(store_root)["problems"][0]
    assert "not reporting" in line
    
    assert line.index("whether it is even up") < line.index("its sync loop is dead")
    assert "tailscale status" in line
    assert "needs no action at all" in line
    
    assert not line.startswith("pc: not reporting for 13h — its sync loop is dead")


def _mark_sleeps(root, uuid, sleeps=True):
    'Write the machine record fleet._sleeps reads, beside the status dir.'
    (root / "machines" / f"{uuid}.toml").write_text(
        f'type = "machine"\nmachine_uuid = "{uuid}"\nsleeps = {str(bool(sleeps)).lower()}\n')


def test_a_machine_recorded_as_sleeping_is_not_a_finding(store_root):
    'test a machine recorded as sleeping is not a finding.'
    _put(store_root, "u1", machine_id="laptop")
    _put(store_root, "u2", machine_id="pc", age_secs=20 * 3600)
    _mark_sleeps(store_root, "u2")
    h = fleet.health(store_root)
    assert h["problems"] == []
    assert h["converged"] is True


def test_a_sleeper_gone_for_a_week_is_still_reported(store_root):
    'Silence is excused, not unbounded: a week is long for a nap, and far more\n    likely to be a machine that never came back up.'
    _put(store_root, "u1", machine_id="pc", age_secs=8 * 86400)
    _mark_sleeps(store_root, "u1")
    line = fleet.health(store_root)["problems"][0]
    assert "sleeps" in line and "8 days" in line


def test_a_machine_not_marked_as_sleeping_still_reports(store_root):
    'The suppression is opt-in per machine. Without the flag nothing changes --\n    a genuinely dead sync loop must still be the alarm it always was.'
    _put(store_root, "u1", machine_id="mirror-a", age_secs=20 * 3600)
    line = fleet.health(store_root)["problems"][0]
    assert "not reporting for 20h" in line


def test_clearing_the_flag_restores_the_finding(store_root):
    _put(store_root, "u1", machine_id="pc", age_secs=20 * 3600)
    _mark_sleeps(store_root, "u1", sleeps=False)
    assert fleet.health(store_root)["problems"] != []


def test_a_sleeping_machine_on_stale_code_is_not_nagged_either(store_root):
    'A machine that is off cannot adopt a release. Reporting its build as a gate\n    failure is the same false alarm wearing different clothes.'
    _put(store_root, "u1", machine_id="pc", code_current=False, age_secs=20 * 3600)
    _mark_sleeps(store_root, "u1")
    assert fleet.health(store_root)["problems"] == []


def test_publish_names_the_server_commit_the_daemon_runs(store_root):
    'test publish names the server commit the daemon runs.'
    fleet.publish(store_root, "u-sha", {"verdict": "healthy", "code_current": True,
                                        "server_commit": "5937e79a"},
                  machine_id="m1", hostname="m1")
    row = json.loads(fleet.status_path(store_root, "u-sha").read_text())
    assert row["server_commit"] == "5937e79a"
    rows = {r["machine_id"]: r for r in fleet.read_all(store_root)}
    assert rows["m1"]["server_commit"] == "5937e79a"

    fleet.publish(store_root, "u-old", {"verdict": "healthy", "code_current": True},
                  machine_id="m2", hostname="m2")
    row = json.loads(fleet.status_path(store_root, "u-old").read_text())
    assert row["server_commit"] is None
