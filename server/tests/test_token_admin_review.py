'Review findings on token_admin: no live orphan token on any failure, strict input, a corrupt\ntable is never overwritten, one live token per machine, and no gateway text echoed.'
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_context import token_admin, token_table

FAKE = '''#!PYTHON
import json, os, sys
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
with open(os.environ["FAKE_OP_LOG"], "a") as handle:
    handle.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin}) + "\\n")
argv = sys.argv[1:]
if argv[:2] == ["item", "get"]:
    body = os.environ.get("FAKE_OP_GET_BODY")
    if body is None:
        sys.stderr.write('[ERROR] "x" isn\\'t an item in the "v" vault\\n')
        sys.exit(1)
    print(body)
    sys.exit(0)
if os.environ.get("FAKE_OP_MODE") == "write_fail":
    sys.stderr.write("SECRETLINE-xyz other item content\\n")
    sys.exit(1)
sys.exit(0)
'''
ISSUE = ["issue", "--id", "laptop", "--machine-id", "laptop", "--scopes", "read"]


@pytest.fixture(autouse=True)
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "fake-op"
    fake.write_text(FAKE.replace("PYTHON", sys.executable))
    fake.chmod(0o755)
    monkeypatch.setenv("TOKEN_ADMIN_OP", str(fake))
    monkeypatch.setenv("FAKE_OP_LOG", str(tmp_path / "op.log"))
    monkeypatch.delenv("FAKE_OP_GET_BODY", raising=False)
    monkeypatch.delenv("FAKE_OP_MODE", raising=False)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN_TABLE", str(tmp_path / "tokens.json"))


def _table(tmp_path: Path) -> Path:
    return tmp_path / "tokens.json"


def _live(tmp_path: Path) -> list[str]:
    path = _table(tmp_path)
    if not path.exists():
        return []
    return [e["id"] for e in json.loads(path.read_text())["tokens"]
            if not e.get("revoked")]


def _calls(tmp_path: Path) -> list[dict]:
    log = tmp_path / "op.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _run(argv: list[str]) -> int:
    try:
        return token_admin.main(argv)
    except (Exception, KeyboardInterrupt) as exc:      
        pytest.fail(f"main raised {exc!r}")





def test_a_missing_gateway_leaves_no_live_token(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("TOKEN_ADMIN_OP", str(tmp_path / "no-such-gateway"))
    assert _run([*ISSUE, "--apply"]) != 0
    assert _live(tmp_path) == []
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("exc", [RuntimeError("boom"), OSError("disk"), KeyboardInterrupt()])
def test_any_failure_while_storing_revokes_the_entry(
        tmp_path: Path, monkeypatch, exc: BaseException) -> None:
    def broken(machine_id: str, secret: str) -> None:
        raise exc

    monkeypatch.setattr(token_admin, "_store_secret", broken)
    with pytest.raises((Exception, KeyboardInterrupt)):
        token_admin.main([*ISSUE, "--apply"])
    assert _live(tmp_path) == []


def test_a_failing_rollback_does_not_hide_the_first_error(
        tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("FAKE_OP_MODE", "write_fail")
    monkeypatch.setattr(token_table, "revoke",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read only")))
    assert _run([*ISSUE, "--apply"]) != 0
    text = capsys.readouterr().err
    assert "1Password" in text and "Traceback" not in text


@pytest.mark.parametrize("body", ['{"fields": null}', "[]", '{"fields": [5]}', '"text"',
                                  "not json", '{"fields": {"a": 1}}'])
def test_an_odd_existing_item_is_refused_and_leaves_no_live_token(
        tmp_path: Path, monkeypatch, body: str) -> None:
    monkeypatch.setenv("FAKE_OP_GET_BODY", body)
    assert _run([*ISSUE, "--apply"]) != 0
    assert _live(tmp_path) == []
    assert [c["argv"][:2] for c in _calls(tmp_path)] == [["item", "get"]]


def test_an_existing_credential_of_another_type_is_stored_concealed(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_OP_GET_BODY", json.dumps({"title": "t", "fields": [
        {"id": "credential", "label": "credential", "type": "STRING", "value": "old"}]}))
    assert _run([*ISSUE, "--apply"]) == 0
    sent = json.loads(_calls(tmp_path)[1]["stdin"])
    field = next(f for f in sent["fields"] if f["label"] == "credential")
    assert field["type"] == "CONCEALED" and field["value"].startswith("acx_laptop_")


def test_no_gateway_text_is_echoed(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("FAKE_OP_MODE", "write_fail")
    assert _run([*ISSUE, "--apply"]) != 0
    seen = capsys.readouterr()
    assert "SECRETLINE" not in seen.out + seen.err and "exit 1" in seen.err





@pytest.mark.parametrize("days", ["0", "-1", "inf", "nan", "1e9", "-0.5"])
def test_expires_days_must_be_a_sane_positive_number(tmp_path: Path, days: str) -> None:
    assert _run([*ISSUE, "--expires-days", days, "--apply"]) != 0
    assert not _table(tmp_path).exists() and _calls(tmp_path) == []


def test_a_sane_expiry_is_still_accepted(tmp_path: Path) -> None:
    assert _run([*ISSUE, "--expires-days", "30", "--apply"]) == 0
    entry = json.loads(_table(tmp_path).read_text())["tokens"][0]
    assert entry["expires"] == pytest.approx(time.time() + 30 * 86400, abs=30)


@pytest.mark.parametrize("machine", ["", "Laptop", "a b", "-x", "a/b", "m1\nx", "x" * 60])
def test_a_machine_id_must_be_a_plain_fleet_id(tmp_path: Path, machine: str) -> None:
    argv = ["issue", "--id", "t1", "--machine-id", machine, "--scopes", "read", "--apply"]
    assert _run(argv) != 0
    assert not _table(tmp_path).exists() and _calls(tmp_path) == []


@pytest.mark.parametrize("machine", ["laptop", "m4", "pc", "server-host", "mirror-a", "rp"])
def test_fleet_machine_ids_are_accepted(tmp_path: Path, machine: str) -> None:
    argv = ["issue", "--id", "t1", "--machine-id", machine, "--scopes", "read", "--apply"]
    assert _run(argv) == 0





def test_a_second_live_token_for_a_machine_is_refused(tmp_path: Path) -> None:
    assert _run([*ISSUE, "--apply"]) == 0
    before = _table(tmp_path).read_bytes()
    calls = len(_calls(tmp_path))
    other = ["issue", "--id", "laptop2", "--machine-id", "laptop", "--scopes", "read", "--apply"]
    assert _run(other) != 0
    assert _table(tmp_path).read_bytes() == before and len(_calls(tmp_path)) == calls


def test_a_revoked_token_does_not_block_a_new_one(tmp_path: Path) -> None:
    assert _run([*ISSUE, "--apply"]) == 0
    assert _run(["revoke", "--id", "laptop", "--apply"]) == 0
    other = ["issue", "--id", "laptop2", "--machine-id", "laptop", "--scopes", "read", "--apply"]
    assert _run(other) == 0


def test_two_issues_for_one_machine_at_once_leave_one_live_token(tmp_path: Path) -> None:
    codes: list[int] = []

    def go(token_id: str) -> None:
        codes.append(token_admin.main(["issue", "--id", token_id, "--machine-id", "laptop",
                                       "--scopes", "read", "--apply"]))

    threads = [threading.Thread(target=go, args=(f"t{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes).count(0) == 1 and len(_live(tmp_path)) == 1





@pytest.mark.parametrize("content", ["{bad", "", "[]", '{"tokens": "x"}', "null"])
def test_a_corrupt_table_is_left_alone(tmp_path: Path, content: str) -> None:
    _table(tmp_path).write_text(content)
    for argv in ([*ISSUE, "--apply"], ["revoke", "--id", "laptop", "--apply"],
                 ["rotate", "--id", "laptop", "--apply"], ["list"]):
        assert _run(argv) != 0
    assert _table(tmp_path).read_text() == content and _calls(tmp_path) == []


def test_token_table_refuses_to_write_over_a_corrupt_table(tmp_path: Path) -> None:
    _table(tmp_path).write_text("{bad")
    with pytest.raises(ValueError, match="not valid"):
        token_table.issue(_table(tmp_path), id="x", scopes=["read"])
    with pytest.raises(ValueError, match="not valid"):
        token_table.list_entries(_table(tmp_path))
    assert _table(tmp_path).read_text() == "{bad"


def test_a_missing_table_is_still_just_empty(tmp_path: Path) -> None:
    assert token_table.list_entries(_table(tmp_path)) == []
    token_table.issue(_table(tmp_path), id="x", scopes=["read"])
    assert [e["id"] for e in token_table.list_entries(_table(tmp_path))] == ["x"]





def test_a_failed_expiry_change_says_the_rotation_is_incomplete(
        tmp_path: Path, monkeypatch, capsys) -> None:
    token_table.issue(_table(tmp_path), id="laptop", machine_id="laptop", scopes=["read"])

    def broken(*a, **k):
        raise ValueError("bad expiry")

    monkeypatch.setattr(token_table, "set_expiry", broken)
    assert _run(["rotate", "--id", "laptop", "--apply"]) != 0
    text = capsys.readouterr().err
    assert "incomplete" in text and "laptop" in text and f"laptop-{time.strftime('%Y%m%d')}" in text
    assert len(_live(tmp_path)) == 2





def test_list_survives_hand_edited_rows(tmp_path: Path, capsys) -> None:
    _table(tmp_path).write_text(json.dumps({"tokens": [
        {"id": "a", "scopes": "abc", "created": "yesterday", "expires": "soon", "revoked": False},
        {"id": "b", "scopes": None, "allowed_ips": "x"}]}))
    assert _run(["list", "--json"]) == 0
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["a"]["scopes"] == [] and rows["a"]["created"] is None
    assert rows["a"]["expires"] is None and rows["b"]["allowed_ips"] == []
