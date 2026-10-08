"token_admin: issue, list, revoke and rotate table tokens, with the plaintext going only to\n1Password through the op gateway (`python -m agent_context.token_admin`).\n\nEvery test drives `main(argv)` against a temp token table and a fake gateway that\nrecords each call's argv and stdin. No test touches the real table, the real vault or a real\n`op` binary."
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

from agent_context import token_admin, token_table

FAKE_OP = '''#!{python}
import json, os, sys
log = os.environ["FAKE_OP_LOG"]
store_path = os.environ["FAKE_OP_STORE"]
fail = os.environ.get("FAKE_OP_FAIL", "")
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
with open(log, "a") as handle:
    handle.write(json.dumps({{"argv": sys.argv[1:], "stdin": stdin}}) + "\\n")
try:
    store = json.load(open(store_path))
except OSError:
    store = {{}}
argv = sys.argv[1:]
if argv[:2] == ["item", "delete"]:
    sys.exit(0)
title = argv[2] if len(argv) > 2 else ""
if argv[:2] == ["item", "get"]:
    if fail == "get":
        sys.stderr.write("[ERROR] network unreachable\\n")
        sys.exit(1)
    if title not in store:
        sys.stderr.write('[ERROR] "%s" isn\\'t an item in the vault\\n' % title)
        sys.exit(1)
    print(json.dumps(store[title]))
    sys.exit(0)
if argv[:2] == ["item", "create"]:
    if fail == "create":
        sys.stderr.write("[ERROR] could not create\\n")
        sys.exit(1)
    body = json.loads(stdin)
    store[body["title"]] = body
elif argv[:2] == ["item", "edit"]:
    if fail == "edit":
        sys.stderr.write("[ERROR] could not edit\\n")
        sys.exit(1)
    store[title] = json.loads(stdin)
json.dump(store, open(store_path, "w"))
'''


@pytest.fixture
def admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    fake = tmp_path / "fake-op"
    fake.write_text(FAKE_OP.format(python=sys.executable))
    fake.chmod(0o755)
    monkeypatch.setenv("TOKEN_ADMIN_OP", str(fake))
    monkeypatch.delenv("TOKEN_ADMIN_VAULT", raising=False)
    monkeypatch.setenv("FAKE_OP_LOG", str(tmp_path / "op.log"))
    monkeypatch.setenv("FAKE_OP_STORE", str(tmp_path / "op-store.json"))
    monkeypatch.delenv("FAKE_OP_FAIL", raising=False)
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN_TABLE", str(tmp_path / "tokens.json"))
    return token_admin


def _table(tmp_path: Path) -> Path:
    return tmp_path / "tokens.json"


def _entries(tmp_path: Path) -> list[dict]:
    return json.loads(_table(tmp_path).read_text())["tokens"]


def _calls(tmp_path: Path) -> list[dict]:
    log = tmp_path / "op.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _items(tmp_path: Path) -> dict:
    path = tmp_path / "op-store.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _secret_of(tmp_path: Path, title: str) -> str:
    item = _items(tmp_path)[title]
    field = next(f for f in item["fields"] if f.get("label") == "credential")
    return field["value"]


ISSUE = ["issue", "--id", "laptop", "--machine-id", "laptop", "--scopes", "read,entity-write"]


def _run(admin: ModuleType, argv: list[str]) -> int:
    return admin.main(argv)


def _out(capsys: pytest.CaptureFixture[str]) -> str:
    seen = capsys.readouterr()
    return seen.out + seen.err





def test_issue_refuses_ls_local_unless_the_script_flag_is_set(tmp_path: Path) -> None:
    table = _table(tmp_path)
    with pytest.raises(ValueError, match="reserved"):
        token_table.issue(table, id="ls-local", scopes=["read"])
    token_table.issue(table, id="ls-local", scopes=["read"], allow_ls_local=True)
    assert [e["id"] for e in _entries(tmp_path)] == ["ls-local"]


@pytest.mark.parametrize("reserved", ["mobile-oauth", "legacy-shared"])
def test_the_script_flag_never_allows_the_other_reserved_ids(
        tmp_path: Path, reserved: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        token_table.issue(_table(tmp_path), id=reserved, scopes=["read"], allow_ls_local=True)
    assert not _table(tmp_path).exists()


def test_set_expiry_only_shortens(tmp_path: Path) -> None:
    table = _table(tmp_path)
    token_table.issue(table, id="a", scopes=["read"], expires=time.time() + 7200)
    token_table.issue(table, id="b", scopes=["read"])
    soon = time.time() + 60
    assert token_table.set_expiry(table, "a", time.time() + 86400) == pytest.approx(
        time.time() + 7200, abs=5)
    assert token_table.set_expiry(table, "b", soon) == soon
    assert token_table.set_expiry(table, "a", soon) == soon
    with pytest.raises(ValueError, match="no token"):
        token_table.set_expiry(table, "missing", soon)


def test_list_entries_returns_the_table_rows(tmp_path: Path) -> None:
    assert token_table.list_entries(_table(tmp_path)) == []
    token_table.issue(_table(tmp_path), id="a", scopes=["read"])
    assert [e["id"] for e in token_table.list_entries(_table(tmp_path))] == ["a"]





def test_issue_defaults_to_a_dry_run(admin, tmp_path: Path, capsys) -> None:
    assert _run(admin, ISSUE) == 0
    text = _out(capsys)
    assert not _table(tmp_path).exists() and _calls(tmp_path) == []
    assert "example.invalid" in text and "agent-context token - laptop" in text
    assert "credential" in text and "acx_" not in text


def test_issue_apply_writes_the_entry_and_stores_the_plaintext_in_1password(
        admin, tmp_path: Path, capsys) -> None:
    assert _run(admin, [*ISSUE, "--allowed-ips", "127.0.0.1", "--apply"]) == 0
    text = _out(capsys)
    entry = _entries(tmp_path)[0]
    assert entry["id"] == "laptop" and entry["machine_id"] == "laptop"
    assert entry["scopes"] == ["read", "entity-write"] and entry["allowed_ips"] == ["127.0.0.1"]
    calls = _calls(tmp_path)
    assert [c["argv"][:2] for c in calls] == [["item", "get"], ["item", "create"]]
    assert "--vault" in calls[1]["argv"] and "example.invalid" in calls[1]["argv"]
    secret = _secret_of(tmp_path, "agent-context token - laptop")
    assert secret.startswith("acx_laptop_")
    info = token_table.verify(secret)
    assert info is not None and info.id == "laptop"
    field = next(f for f in _items(tmp_path)["agent-context token - laptop"]["fields"]
                 if f.get("label") == "credential")
    assert field["type"] == "CONCEALED"
    
    assert all(secret not in " ".join(c["argv"]) for c in calls)
    assert secret in calls[1]["stdin"]
    assert secret not in text and hashlib.sha256(secret.split("_", 2)[2].encode()).hexdigest() \
        not in text
    assert secret not in _table(tmp_path).read_text()
    assert stat.S_IMODE(os.stat(_table(tmp_path)).st_mode) == 0o600


def test_issue_updates_an_existing_item_in_place(admin, tmp_path: Path) -> None:
    (tmp_path / "op-store.json").write_text(json.dumps({"agent-context token - laptop": {
        "title": "agent-context token - laptop", "category": "API_CREDENTIAL",
        "fields": [{"id": "credential", "label": "credential", "type": "CONCEALED",
                    "value": "old"}, {"id": "notes", "label": "notes", "value": "keep"}]}}))
    assert _run(admin, [*ISSUE, "--apply"]) == 0
    assert [c["argv"][:2] for c in _calls(tmp_path)] == [["item", "get"], ["item", "edit"]]
    item = _items(tmp_path)["agent-context token - laptop"]
    assert _secret_of(tmp_path, "agent-context token - laptop").startswith("acx_laptop_")
    assert any(f.get("label") == "notes" and f["value"] == "keep" for f in item["fields"])


def test_issue_honors_a_vault_override(admin, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TOKEN_ADMIN_VAULT", "Other")
    assert _run(admin, [*ISSUE, "--apply"]) == 0
    assert "Other" in _calls(tmp_path)[1]["argv"]


def test_a_failed_1password_write_revokes_the_new_entry(admin, tmp_path: Path, capsys,
                                                       monkeypatch) -> None:
    monkeypatch.setenv("FAKE_OP_FAIL", "create")
    assert _run(admin, [*ISSUE, "--apply"]) != 0
    text = _out(capsys)
    assert _entries(tmp_path)[0]["revoked"] is True
    assert "acx_" not in text
    assert token_table.lookup("laptop") is None


def test_a_gateway_error_that_is_not_a_missing_item_stops_before_any_write(
        admin, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_OP_FAIL", "get")
    assert _run(admin, [*ISSUE, "--apply"]) != 0
    assert [c["argv"][:2] for c in _calls(tmp_path)] == [["item", "get"]]
    assert _items(tmp_path) == {}
    assert _entries(tmp_path)[0]["revoked"] is True


@pytest.mark.parametrize("reserved", ["mobile-oauth", "legacy-shared"])
def test_reserved_ids_are_refused(admin, tmp_path: Path, reserved: str) -> None:
    argv = ["issue", "--id", reserved, "--machine-id", "x", "--scopes", "read", "--apply"]
    assert _run(admin, argv) != 0
    assert not _table(tmp_path).exists() and _calls(tmp_path) == []


def test_ls_local_is_issued_bound_to_loopback(admin, tmp_path: Path) -> None:
    argv = ["issue", "--id", "ls-local", "--machine-id", "server-host", "--scopes",
            "read,entity-write,protected-write", "--apply"]
    assert _run(admin, argv) == 0
    assert _entries(tmp_path)[0]["allowed_ips"] == ["127.0.0.1", "::1"]


def test_ls_local_cannot_be_given_other_addresses(admin, tmp_path: Path) -> None:
    argv = ["issue", "--id", "ls-local", "--machine-id", "server-host", "--scopes", "read",
            "--allowed-ips", "10.0.0.1", "--apply"]
    assert _run(admin, argv) != 0
    assert not _table(tmp_path).exists() and _calls(tmp_path) == []


@pytest.mark.parametrize("argv", [
    ["issue", "--id", "x", "--machine-id", "x", "--scopes", "root", "--apply"],
    ["issue", "--id", "x", "--machine-id", "x", "--scopes", "read", "--allowed-ips", "nope",
     "--apply"],
    ["issue", "--id", "Bad_Id", "--machine-id", "x", "--scopes", "read", "--apply"],
])
def test_invalid_input_writes_nothing(admin, tmp_path: Path, argv: list[str]) -> None:
    assert _run(admin, argv) != 0
    assert not _table(tmp_path).exists() and _calls(tmp_path) == []


def test_issue_of_an_existing_id_is_refused_before_any_gateway_call(
        admin, tmp_path: Path) -> None:
    token_table.issue(_table(tmp_path), id="laptop", scopes=["read"])
    before = _table(tmp_path).read_bytes()
    assert _run(admin, [*ISSUE, "--apply"]) != 0
    assert _calls(tmp_path) == [] and _table(tmp_path).read_bytes() == before


def test_expires_days_sets_an_expiry(admin, tmp_path: Path) -> None:
    assert _run(admin, [*ISSUE, "--expires-days", "30", "--apply"]) == 0
    assert _entries(tmp_path)[0]["expires"] == pytest.approx(time.time() + 30 * 86400, abs=30)





def test_list_shows_the_fields_and_no_secret(admin, tmp_path: Path, capsys) -> None:
    secret = token_table.issue(_table(tmp_path), id="laptop", machine_uuid="U-1",
                               machine_id="laptop", scopes=["read"], allowed_ips=["127.0.0.1"],
                               expires=time.time() + 3600)
    token_table.issue(_table(tmp_path), id="old", scopes=["read"])
    token_table.revoke(_table(tmp_path), "old")
    assert _run(admin, ["list", "--json"]) == 0
    text = _out(capsys)
    rows = {r["id"]: r for r in json.loads(text)}
    assert rows["laptop"]["machine_id"] == "laptop" and rows["laptop"]["machine_uuid"] == "U-1"
    assert rows["laptop"]["scopes"] == ["read"] and rows["laptop"]["allowed_ips"] == ["127.0.0.1"]
    assert rows["laptop"]["revoked"] is False and rows["old"]["revoked"] is True
    assert rows["laptop"]["created"] and rows["laptop"]["expires"]
    assert rows["old"]["expires"] is None
    assert "sha256" not in text and secret.split("_", 2)[2] not in text
    assert _calls(tmp_path) == []


def test_list_text_output_has_no_secret_or_hash(admin, tmp_path: Path, capsys) -> None:
    secret = token_table.issue(_table(tmp_path), id="laptop", scopes=["read"])
    assert _run(admin, ["list"]) == 0
    text = _out(capsys)
    assert "laptop" in text and secret.split("_", 2)[2] not in text and "sha256" not in text


def test_list_of_a_missing_table_lists_nothing(admin, capsys) -> None:
    assert _run(admin, ["list", "--json"]) == 0
    assert json.loads(_out(capsys)) == []





def test_revoke_defaults_to_a_dry_run(admin, tmp_path: Path) -> None:
    token_table.issue(_table(tmp_path), id="laptop", scopes=["read"])
    before = _table(tmp_path).read_bytes()
    assert _run(admin, ["revoke", "--id", "laptop"]) == 0
    assert _table(tmp_path).read_bytes() == before and _calls(tmp_path) == []


def test_revoke_apply_ends_the_token_and_leaves_the_item(admin, tmp_path: Path) -> None:
    secret = token_table.issue(_table(tmp_path), id="laptop", scopes=["read"])
    assert _run(admin, ["revoke", "--id", "laptop", "--apply"]) == 0
    assert token_table.verify(secret) is None and _calls(tmp_path) == []


def test_revoke_of_an_unknown_id_fails_clearly(admin, tmp_path: Path, capsys) -> None:
    assert _run(admin, ["revoke", "--id", "nope", "--apply"]) != 0
    assert "nope" in _out(capsys)


def test_revoke_of_a_revoked_id_says_so(admin, tmp_path: Path, capsys) -> None:
    token_table.issue(_table(tmp_path), id="laptop", scopes=["read"])
    token_table.revoke(_table(tmp_path), "laptop")
    assert _run(admin, ["revoke", "--id", "laptop", "--apply"]) == 0
    assert "already revoked" in _out(capsys)





def _seed(tmp_path: Path, **kw) -> str:
    return token_table.issue(_table(tmp_path), id="laptop", machine_uuid="U-1",
                             machine_id="laptop", scopes=["read", "entity-write"],
                             allowed_ips=["127.0.0.1"], **kw)


def test_rotate_defaults_to_a_dry_run(admin, tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    before = _table(tmp_path).read_bytes()
    assert _run(admin, ["rotate", "--id", "laptop"]) == 0
    assert _table(tmp_path).read_bytes() == before and _calls(tmp_path) == []
    assert "acx_" not in _out(capsys)


def test_rotate_apply_issues_a_new_entry_and_overlaps_the_old_one(
        admin, tmp_path: Path, capsys) -> None:
    old = _seed(tmp_path)
    assert _run(admin, ["rotate", "--id", "laptop", "--apply"]) == 0
    text = _out(capsys)
    rows = {e["id"]: e for e in _entries(tmp_path)}
    new_id = f"laptop-{time.strftime('%Y%m%d')}"
    assert set(rows) == {"laptop", new_id}
    assert rows[new_id]["machine_id"] == "laptop" and rows[new_id]["machine_uuid"] == "U-1"
    assert rows[new_id]["scopes"] == ["read", "entity-write"]
    assert rows[new_id]["allowed_ips"] == ["127.0.0.1"] and rows[new_id]["expires"] is None
    assert rows["laptop"]["expires"] == pytest.approx(time.time() + 24 * 3600, abs=30)
    new = _secret_of(tmp_path, "agent-context token - laptop")
    assert new.startswith(f"acx_{new_id}_")
    assert token_table.verify(new) is not None and token_table.verify(old) is not None
    assert [c["argv"][:2] for c in _calls(tmp_path)] == [["item", "get"], ["item", "create"]]
    assert new not in text and old not in text


def test_rotate_updates_the_same_item(admin, tmp_path: Path) -> None:
    _run(admin, [*ISSUE, "--apply"])
    first = _secret_of(tmp_path, "agent-context token - laptop")
    assert _run(admin, ["rotate", "--id", "laptop", "--apply"]) == 0
    assert _secret_of(tmp_path, "agent-context token - laptop") != first
    assert [c["argv"][:2] for c in _calls(tmp_path)[2:]] == [["item", "get"], ["item", "edit"]]


def test_the_old_token_stops_working_after_the_overlap(
        admin, tmp_path: Path, monkeypatch) -> None:
    old = _seed(tmp_path)
    assert _run(admin, ["rotate", "--id", "laptop", "--overlap-hours", "2", "--apply"]) == 0
    assert token_table.verify(old) is not None
    real = time.time
    monkeypatch.setattr(token_table.time, "time", lambda: real() + 3 * 3600)
    assert token_table.verify(old) is None


def test_rotate_never_extends_an_earlier_expiry(admin, tmp_path: Path) -> None:
    _seed(tmp_path, expires=time.time() + 7200)
    assert _run(admin, ["rotate", "--id", "laptop", "--apply"]) == 0
    old = next(e for e in _entries(tmp_path) if e["id"] == "laptop")
    assert old["expires"] == pytest.approx(time.time() + 7200, abs=30)


@pytest.mark.parametrize("hours,ok", [("1", True), ("168", True), ("0", False),
                                      ("169", False), ("-3", False), ("x", False)])
def test_the_overlap_is_bounded(admin, tmp_path: Path, hours: str, ok: bool) -> None:
    _seed(tmp_path)
    try:
        code = _run(admin, ["rotate", "--id", "laptop", "--overlap-hours", hours, "--apply"])
    except SystemExit as exc:                 
        code = int(exc.code or 0)
    assert (code == 0) is ok
    if not ok:
        assert [e["id"] for e in _entries(tmp_path)] == ["laptop"] and _calls(tmp_path) == []


def test_a_same_day_rotation_takes_a_counter(admin, tmp_path: Path) -> None:
    _seed(tmp_path)
    assert _run(admin, ["rotate", "--id", "laptop", "--apply"]) == 0
    day = time.strftime("%Y%m%d")
    assert _run(admin, ["rotate", "--id", f"laptop-{day}", "--apply"]) == 0
    ids = {e["id"] for e in _entries(tmp_path)}
    assert ids == {"laptop", f"laptop-{day}", f"laptop-{day}-2"}


def test_a_failed_1password_write_undoes_a_rotation(admin, tmp_path: Path,
                                                    monkeypatch) -> None:
    _seed(tmp_path)
    monkeypatch.setenv("FAKE_OP_FAIL", "create")
    assert _run(admin, ["rotate", "--id", "laptop", "--apply"]) != 0
    rows = {e["id"]: e for e in _entries(tmp_path)}
    assert rows["laptop"]["expires"] is None and rows["laptop"]["revoked"] is False
    new = next(r for i, r in rows.items() if i != "laptop")
    assert new["revoked"] is True


@pytest.mark.parametrize("missing", ["nope"])
def test_rotate_of_an_unknown_or_revoked_id_fails(admin, tmp_path: Path, missing: str) -> None:
    token_table.issue(_table(tmp_path), id="gone", scopes=["read"])
    token_table.revoke(_table(tmp_path), "gone")
    for target in (missing, "gone"):
        assert _run(admin, ["rotate", "--id", target, "--apply"]) != 0
    assert _calls(tmp_path) == []





def test_the_default_gateway_is_the_op_wrapper_in_local_bin(
        admin, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOKEN_ADMIN_OP")
    assert admin.gateway_path() == str(Path.home() / ".local" / "bin" / "op")


def test_no_scenario_ever_deletes_an_item_or_uses_more_than_two_calls(
        admin, tmp_path: Path) -> None:
    _run(admin, [*ISSUE, "--apply"])
    per_issue = len(_calls(tmp_path))
    _run(admin, ["rotate", "--id", "laptop", "--apply"])
    _run(admin, ["revoke", "--id", "laptop", "--apply"])
    _run(admin, ["list"])
    calls = _calls(tmp_path)
    assert per_issue <= 2 and len(calls) <= 4
    assert all(c["argv"][:2] != ["item", "delete"] for c in calls)
    assert all(c["argv"][0] == "item" for c in calls)


def test_a_dry_run_leaves_an_existing_table_byte_identical(admin, tmp_path: Path) -> None:
    _seed(tmp_path)
    before = _table(tmp_path).read_bytes()
    for argv in (["issue", "--id", "other", "--machine-id", "m4", "--scopes", "read"],
                 ["rotate", "--id", "laptop"],
                 ["revoke", "--id", "laptop"]):
        assert _run(admin, argv) == 0
    assert _table(tmp_path).read_bytes() == before and _calls(tmp_path) == []


def test_the_table_argument_overrides_the_default_path(admin, tmp_path: Path) -> None:
    other = tmp_path / "other.json"
    argv = [*ISSUE, "--table", str(other), "--apply"]
    assert _run(admin, argv) == 0
    assert other.exists() and not _table(tmp_path).exists()
