"adoption.py — delivery is not adoption.\n\nThese tests pin the probe's two facts and, more importantly, its two silences: a\nmissing stamp and an unaskable lspd must read as UNKNOWN, never as healthy. Inventing\ngood news about a machine that did not answer is the exact failure being fixed."
import json

from agent_context import adoption



def test_projection_reads_the_stamp_and_ages_it(tmp_path):
    p = tmp_path / "materialize-stamp.json"
    p.write_text(json.dumps({"at": 1_800_000_000, "commit": "abc1234",
                             "findings": 0}))
    got = adoption.projection(stamp_path=p, now=1_800_003_600.0)
    assert got["at"] == 1_800_000_000
    assert got["commit"] == "abc1234"
    assert got["age_secs"] == 3600


def test_a_missing_stamp_is_unknown_not_zero(tmp_path):
    "A machine that has never projected since the stamp existed looks exactly like\n    one that projected very long ago. Reporting either as 'age 0' would say the\n    machine is current on no evidence at all."
    got = adoption.projection(stamp_path=tmp_path / "nope.json")
    assert got == {"at": None, "commit": None, "age_secs": None, "findings": None}


def test_a_corrupt_stamp_is_unknown_rather_than_an_exception(tmp_path):
    'This runs inside the sync loop. A half-written file must not be able to break\n    the publish that carries the diagnostic.'
    p = tmp_path / "materialize-stamp.json"
    p.write_text("{ not json")
    assert adoption.projection(stamp_path=p)["at"] is None
    p.write_text(json.dumps({"commit": "abc1234"}))     
    assert adoption.projection(stamp_path=p)["at"] is None


def test_the_projected_commit_is_carried(tmp_path):
    'A machine that projected an hour ago from a commit fifty behind is a different\n    fault from one that has not projected in a week, and a timestamp cannot tell them\n    apart.'
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"at": 1_800_000_000, "commit": "deadbee"}))
    assert adoption.projection(stamp_path=p, now=1_800_000_001)["commit"] == "deadbee"




def _fake_lspd(tmp_path, stdout, sleep=None):
    'A stand-in lspd.py that prints a canned --status and exits.'
    p = tmp_path / "lspd.py"
    lines = ["import sys, time"]
    if sleep:
        lines.append(f"time.sleep({sleep})")
    lines.append(f"sys.stdout.write({stdout!r})")
    p.write_text("\n".join(lines) + "\n")
    return p


CURRENT = (
    "LIVE   csharp-ls-f3000ef5      canary=ok probe=X baseline=2 last=2 "
    "restarts=0 clients=1\n"
)
STALE_TWO = CURRENT + (
    "LIVE   kotlin-lsp-cc660802     canary=ok probe=Sel baseline=31 last=31 "
    "restarts=0 clients=2\n"
    "                               code=STALE (predates the code field) -- run "
    "`lspd.py --upgrade --key kotlin-lsp`\n"
    "LIVE   tsgo-aa11bb22           canary=ok probe=Y baseline=4 last=4 "
    "restarts=0 clients=1\n"
    "                               code=STALE (running aaa, on disk bbb) -- run "
    "`lspd.py --upgrade --key tsgo`\n"
)


def test_stale_daemons_are_named_from_lspds_own_remedy_line(tmp_path):
    "The key is taken from lspd's own `--upgrade --key K` text rather than\n    re-derived from a socket filename here, so there is one place that decides what a\n    daemon is called."
    lspd = _fake_lspd(tmp_path, STALE_TWO)
    assert adoption.stale_daemons(lspd_path=lspd) == ["kotlin-lsp", "tsgo"]


def test_all_current_daemons_report_an_empty_list_not_none(tmp_path):
    "[] is a real answer -- 'asked, everything is current' -- and must be\n    distinguishable from 'could not ask'."
    lspd = _fake_lspd(tmp_path, CURRENT)
    assert adoption.stale_daemons(lspd_path=lspd) == []


def test_no_lspd_installed_is_unknown(tmp_path):
    'Not every machine runs language servers. Absent is not all-clear.'
    assert adoption.stale_daemons(lspd_path=tmp_path / "absent.py") is None


def test_a_probe_that_times_out_is_unknown_not_all_clear(tmp_path):
    '--status connects to each daemon socket, so a host with wedged daemons can\n    hang. Capped hard, because this runs in the sync loop -- and the capped result is\n    UNKNOWN, since a timeout proves nothing about the daemons.'
    lspd = _fake_lspd(tmp_path, CURRENT, sleep=5)
    assert adoption.stale_daemons(lspd_path=lspd, timeout=0.5) is None


def test_no_daemons_at_all_is_all_clear(tmp_path):
    lspd = _fake_lspd(tmp_path, "no daemons\n")
    assert adoption.stale_daemons(lspd_path=lspd) == []




def test_probe_never_raises_and_reports_unknowns_as_unknown(tmp_path, monkeypatch):
    "The contract that lets this live inside the sync loop. Anything that throws\n    must degrade to 'unknown', never propagate: a diagnostic that can break the sync\n    it rides on is worse than no diagnostic."
    def boom(*a, **k):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(adoption, "projection", boom)
    monkeypatch.setattr(adoption, "stale_daemons", boom)
    got = adoption.probe()
    assert got["stale_daemons"] is None
    assert got["projection"]["at"] is None
