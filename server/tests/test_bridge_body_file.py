'A remote bridge reads `body_path` off its own disk and sends the text as `body`.'
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCRequest

from agent_context import daemon

BODY = "# ledger\n\nrow 1 — ünïcode ✅\n"


def call(arguments, method="tools/call"):
    request = JSONRPCRequest(jsonrpc="2.0", id=7, method=method,
                             params={"name": "upsert_doc", "arguments": arguments})
    return SessionMessage(JSONRPCMessage(request))


def sent_arguments(item):
    return item.message.root.params["arguments"]


def test_the_file_is_read_here_and_sent_as_body(tmp_path):
    f = tmp_path / "LEDGER.md"
    f.write_text(BODY, encoding="utf-8")
    out = daemon._with_body_file(call({"path": "m/LEDGER.md", "body_path": str(f)}))
    assert sent_arguments(out) == {"path": "m/LEDGER.md", "body": BODY}
    assert out.message.root.id == 7


def test_a_missing_file_goes_through_for_the_daemon_to_name(tmp_path):
    item = call({"path": "m/x.md", "body_path": str(tmp_path / "absent.md")})
    assert daemon._with_body_file(item) is item


def test_body_and_body_path_together_go_through_unchanged(tmp_path):
    f = tmp_path / "a.md"
    f.write_text(BODY, encoding="utf-8")
    item = call({"path": "m/a.md", "body": "typed", "body_path": str(f)})
    assert daemon._with_body_file(item) is item


def test_a_relative_path_and_a_file_that_is_not_text_go_through_unchanged(tmp_path):
    relative = call({"path": "m/a.md", "body_path": "notes/a.md"})
    assert daemon._with_body_file(relative) is relative
    raw = tmp_path / "raw.bin"
    raw.write_bytes(b"\xff\xfe\x00bad")
    binary = call({"path": "m/b.md", "body_path": str(raw)})
    assert daemon._with_body_file(binary) is binary


def test_a_file_over_the_limit_goes_through_unchanged(tmp_path, monkeypatch):
    f = tmp_path / "big.md"
    f.write_text("x" * 64, encoding="utf-8")
    monkeypatch.setattr(daemon, "_BODY_FILE_MAX_BYTES", 16)
    item = call({"path": "m/big.md", "body_path": str(f)})
    assert daemon._with_body_file(item) is item


def test_other_calls_are_left_alone():
    plain = call({"path": "m/a.md", "body": "typed"})
    assert daemon._with_body_file(plain) is plain
    other = call({"body_path": "/tmp/x"}, method="tools/list")
    assert daemon._with_body_file(other) is other


def test_the_limit_matches_the_servers():
    from agent_context import docs
    assert daemon._BODY_FILE_MAX_BYTES == docs.BODY_FILE_MAX_BYTES
