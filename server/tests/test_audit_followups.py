'test audit followups.'
import json

import pytest

from agent_context import daemon as D
from agent_context import fstools as T



@pytest.mark.parametrize("name,lang", [("verify-lint.py", "py"), ("wt-finish.sh", "sh"),
                                       ("bundle.js", "node"), ("verify-lint.py", "sh")])
def test_upsert_script_refuses_a_name_carrying_an_extension(store, name, lang):
    with pytest.raises(ValueError) as ei:
        T.upsert_script(store, name, "echo hi", language=lang)
    assert "must not end in a file extension" in str(ei.value)


def test_upsert_hook_refuses_a_name_carrying_an_extension(store):
    with pytest.raises(ValueError):
        T.upsert_hook(store, "guard-git-write.py", "PreToolUse", "exit 0")


def test_upsert_script_accepts_an_extensionless_name(store):
    r = T.upsert_script(store, "verify-lint", "print('hi')", language="py")
    assert r["name"] == "verify-lint"
    p = store.root + "/global/scripts/verify-lint.py"
    assert open(p).read() == "print('hi')"


def test_a_dotted_name_that_is_not_an_extension_still_works(store):
    'Only a KNOWN extension is refused — a dotted name is otherwise legal.'
    r = T.upsert_script(store, "example.invalid-deploy", "echo hi")
    assert r["name"] == "example.invalid-deploy"



def test_retry_delay_is_flat_while_healthy_and_below_the_alert_threshold():
    assert D.sync_retry_delay(300, streak=0) == 300.0
    assert D.sync_retry_delay(300, streak=D._BACKOFF_AFTER_CYCLES - 1) == 300.0


def test_retry_delay_backs_off_after_the_alert_has_fired_and_caps():
    n = D._BACKOFF_AFTER_CYCLES
    assert D.sync_retry_delay(300, streak=n) == 600.0
    assert D.sync_retry_delay(300, streak=n + 1) == 1200.0
    assert D.sync_retry_delay(300, streak=n + 50) == D._BACKOFF_MAX_SECS


def test_backoff_only_starts_after_a_human_has_been_paged():
    'test backoff only starts after a human has been paged.'
    assert D._UNHEALTHY_NOTIFY_CYCLES == 2
    assert D._BACKOFF_AFTER_CYCLES >= D._UNHEALTHY_NOTIFY_CYCLES
    total = sum(D.sync_retry_delay(300, streak=s) for s in range(D._BACKOFF_AFTER_CYCLES))
    assert total == 300.0 * D._BACKOFF_AFTER_CYCLES



def _fake_store_with_script(tmp_path, body="import sys\nsys.exit(0)\n"):
    d = tmp_path / "ctx" / "global" / "scripts"
    d.mkdir(parents=True)
    (d / "home-materialize.py").write_text(body)
    return str(tmp_path / "ctx")


def test_converge_home_runs_the_store_script(tmp_path, monkeypatch):
    root = _fake_store_with_script(
        tmp_path,
        f"from pathlib import Path\nPath(r'{tmp_path}/ran.txt').write_text('ran')\n")
    monkeypatch.setattr(D, "_HOME_CONVERGED_AT", 0.0)
    assert D.maybe_converge_home(root, now=1000.0) == "converged"
    assert (tmp_path / "ran.txt").is_file()


def test_converge_home_is_throttled(tmp_path, monkeypatch):
    root = _fake_store_with_script(tmp_path)
    monkeypatch.setattr(D, "_HOME_CONVERGED_AT", 0.0)
    assert D.maybe_converge_home(root, now=1000.0) == "converged"
    assert D.maybe_converge_home(root, now=1000.0 + D._HOME_CONVERGE_SECS - 1) is None
    assert D.maybe_converge_home(root, now=1000.0 + D._HOME_CONVERGE_SECS + 1) == "converged"


def test_converge_home_swallows_a_failing_script(tmp_path, monkeypatch):
    root = _fake_store_with_script(tmp_path, "import sys\nsys.exit(3)\n")
    monkeypatch.setattr(D, "_HOME_CONVERGED_AT", 0.0)
    assert D.maybe_converge_home(root, now=1000.0) is None      


def test_converge_home_stamps_before_running_so_a_failure_cannot_loop(tmp_path, monkeypatch):
    root = _fake_store_with_script(tmp_path, "import sys\nsys.exit(3)\n")
    monkeypatch.setattr(D, "_HOME_CONVERGED_AT", 0.0)
    D.maybe_converge_home(root, now=1000.0)
    assert D.maybe_converge_home(root, now=1001.0) is None
    assert D._HOME_CONVERGED_AT == 1000.0


def test_converge_home_is_a_noop_without_the_script(tmp_path, monkeypatch):
    (tmp_path / "ctx").mkdir()
    monkeypatch.setattr(D, "_HOME_CONVERGED_AT", 0.0)
    assert D.maybe_converge_home(str(tmp_path / "ctx"), now=1000.0) is None



def test_sync_failure_stamps_the_error_time_and_success_clears_it(tmp_path, monkeypatch):
    info = tmp_path / "daemon.info"
    monkeypatch.setattr(D, "_info_path", lambda: info)
    monkeypatch.setattr(D, "_STARTED_VERSION", 1.0)

    D.record_sync_failure("push refused", now=500.0)
    d = json.loads(info.read_text())
    assert d["last_sync_error"] == "push refused"
    assert d["last_sync_error_at"] == 500.0
    assert d["last_sync_attempt"] == 500.0

    D.record_sync_success(now=900.0)
    d = json.loads(info.read_text())
    assert d["last_sync_error"] is None and d["last_sync_error_at"] is None


def test_a_stale_error_is_distinguishable_from_this_cycles_error(tmp_path, monkeypatch):
    'The whole point: the reader can age the string against the last attempt.'
    info = tmp_path / "daemon.info"
    monkeypatch.setattr(D, "_info_path", lambda: info)
    monkeypatch.setattr(D, "_STARTED_VERSION", 1.0)
    D.record_sync_failure("push refused", now=500.0)
    D.record_sync_failure("push refused", now=800.0)
    d = json.loads(info.read_text())
    assert d["last_sync_error_at"] == d["last_sync_attempt"] == 800.0
