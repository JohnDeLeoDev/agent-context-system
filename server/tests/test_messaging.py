'policy: inter-agent messaging in the daemon.\n\n  - an address is a session connected to the daemon; its claim supplies the name and the ref,\n    and the last claim seen is kept once the claim file expires;\n  - `to` takes a bare name, `name [ref]` or a ref, and an ambiguous name is refused with the\n    candidates;\n  - a recipient whose bridge declared a wake route gets the message pushed down its event\n    stream and nothing is stored; any other recipient reads it from its mailbox;\n  - size, rate and mailbox caps refuse with a reason.'
import json
import time
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from mcp.server.streamable_http import GET_STREAM_KEY

from agent_context import claims, messaging, peer_wake
from agent_context import server as S
from agent_context.identity import Identity

HOST, HOST_UUID = "server-host", "uuid-ls"
LAPTOP_UUID = "uuid-laptop"
KEY_A = "a" * 32
KEY_B = "b" * 32


def _local_claim(session: str, pid: int, **fields: Any) -> None:
    d = claims.claims_dir()
    d.mkdir(parents=True, exist_ok=True)
    rec = {"session": session, "pids": [pid], "last_seen": time.time(), "started": time.time(),
           **fields}
    (d / f"{session}.json").write_text(json.dumps(rec))


def _remote_claim(key: str, **fields: Any) -> None:
    who = Identity(machine_uuid=LAPTOP_UUID, machine_id="laptop", platform="darwin",
                   home="/Users/user", session_key=key)
    assert claims.ensure_remote_claim(who, **fields)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(messaging, "_SENDS", {})
    monkeypatch.setattr(claims, "_ancestors", lambda pid, limit=8, proc="/proc": [])


def _roster(owners: list[dict], now: float | None = None) -> list[dict]:
    return messaging.roster(owners, HOST, HOST_UUID, now=now)




def test_a_name_is_the_project_plus_the_worktree() -> None:
    assert messaging.derive_name({"project": "example-api"}) == "example-api"
    assert messaging.derive_name({"project": "example-api", "worktree": "fix-auth"}) == "example-api:fix-auth"
    assert messaging.derive_name({"cwd": "/home/user/notes/"}) == "notes"
    assert messaging.derive_name({"machine": "rp"}) == "rp"


def test_the_roster_names_local_and_relay_sessions_from_their_claims() -> None:
    _local_claim("11112222-aaaa", 4242, project="agent-context", cwd="/home/user/.agent-context")
    _remote_claim(KEY_A, project="example-api", cwd="/Users/user/Developer/example-api")
    got = _roster([
        {"sid": "s1", "pid": 4242, "connected": True, "wake": "claude-code"},
        {"sid": "s2", "session_key": KEY_A, "machine_uuid": LAPTOP_UUID, "machine_id": "laptop",
         "connected": True, "wake": None},
    ])
    assert [(a["name"], a["ref"], a["machine"], a["wake"]) for a in got] == [
        ("agent-context", "11112222", HOST, "claude-code"),
        ("example-api", "aaaaaaaa", "laptop", None),
    ]
    assert messaging.describe(got[0]).startswith("agent-context [11112222] · server-host · ")


def test_a_connected_session_keeps_its_name_after_its_claim_expires() -> None:
    _local_claim("11112222-aaaa", 4242, project="agent-context")
    owner = {"sid": "s1", "pid": 4242, "connected": True, "wake": None}
    assert _roster([owner])[0]["name"] == "agent-context"
    later = time.time() + claims.TTL_SECS + 60
    assert claims.read_live(now=later) == []
    got = _roster([owner], now=later)
    assert (got[0]["name"], got[0]["ref"]) == ("agent-context", "11112222")


def test_a_session_with_no_claim_is_addressed_by_its_bridge() -> None:
    got = _roster([{"sid": "s1", "pid": 77, "connected": True, "wake": None},
                   {"sid": "s2", "session_key": KEY_B, "machine_uuid": LAPTOP_UUID,
                    "machine_id": "laptop", "connected": True, "wake": None}])
    assert messaging.describe(got[1]) == "laptop [bbbbbbbb] · laptop"   
    assert [(a["name"], a["ref"]) for a in got] == [(HOST, "p77"), ("laptop", "bbbbbbbb")]


def test_two_unclaimed_sessions_with_long_pids_keep_distinct_refs() -> None:
    assert claims.ensure_server_claim(858123, cwd="/home/user/notes")
    got = _roster([{"sid": "s1", "pid": 858123, "connected": True, "wake": None},
                   {"sid": "s2", "pid": 858125, "connected": True, "wake": None}])
    assert [(a["name"], a["ref"]) for a in got] == [("notes", "p858123"), (HOST, "p858125")]


def test_two_mcp_sessions_of_one_claim_are_one_agent_reached_through_the_connected_one() -> None:
    _local_claim("11112222-aaaa", 4242, project="agent-context")
    got = _roster([{"sid": "old", "pid": 4242, "connected": False, "wake": None},
                   {"sid": "new", "pid": 4242, "connected": True, "wake": "claude-code"}])
    assert len(got) == 1
    assert (got[0]["sid"], got[0]["wake"], got[0]["sids"]) == ("new", "claude-code", ["old", "new"])


def test_two_connected_bridges_of_one_session_are_reached_through_the_one_that_wakes() -> None:
    
    _local_claim("11112222-aaaa", 4242, project="agent-context")
    for owners in ([{"sid": "plain", "pid": 4242, "connected": True, "wake": None, "notice": True},
                    {"sid": "ext", "pid": 4242, "connected": True, "wake": "pi"}],
                   [{"sid": "ext", "pid": 4242, "connected": True, "wake": "pi"},
                    {"sid": "plain", "pid": 4242, "connected": True, "wake": None, "notice": True}]):
        got = _roster(owners)
        assert len(got) == 1
        assert (got[0]["sid"], got[0]["wake"]) == ("ext", "pi")




def _agents() -> list[dict]:
    _local_claim("11112222-aaaa", 1, project="store")
    _local_claim("33334444-bbbb", 2, project="example-api")
    _local_claim("55556666-cccc", 3, project="example-api")
    return _roster([{"sid": f"s{p}", "pid": p, "connected": True, "wake": None} for p in (1, 2, 3)])


def test_to_takes_a_bare_name_a_name_with_its_ref_or_a_ref() -> None:
    agents = _agents()
    me = agents[1]
    assert messaging.resolve(agents, "store", me)["ref"] == "11112222"
    assert messaging.resolve(agents, "example-api [55556666]", me)["ref"] == "55556666"
    assert messaging.resolve(agents, "55556666", me)["ref"] == "55556666"
    
    assert messaging.resolve(agents, "example-api", me)["ref"] == "55556666"


def test_an_ambiguous_name_is_refused_with_the_candidates() -> None:
    agents = _agents()
    with pytest.raises(messaging.Refused) as exc:
        messaging.resolve(agents, "example-api", agents[0])
    assert "example-api [33334444]" in str(exc.value) and "example-api [55556666]" in str(exc.value)


def test_an_unknown_name_an_empty_one_and_the_sender_itself_are_refused() -> None:
    agents = _agents()
    with pytest.raises(messaging.Refused, match="no live session found"):
        messaging.resolve(agents, "nobody", agents[0])
    with pytest.raises(messaging.Refused, match="empty"):
        messaging.resolve(agents, "  ", agents[0])
    with pytest.raises(messaging.Refused, match="this session"):
        messaging.resolve(agents, "store [11112222]", agents[0])
    with pytest.raises(messaging.Refused, match="no live session found"):
        messaging.resolve(agents, "example-api [11112222]", agents[1])   


def test_the_envelope_carries_the_reply_address() -> None:
    text = messaging.envelope('odd "name" [11112222]', "hello")
    assert text == ('<cross-session-message from="odd &quot;name&quot; [11112222]" via="agent-context">\n'
                    "hello\n</cross-session-message>")




def test_an_oversized_message_and_a_sender_past_its_rate_are_refused() -> None:
    with pytest.raises(messaging.Refused, match="cap"):
        messaging.check_send("k", "x" * (messaging.MAX_MESSAGE_CHARS + 1))
    for _ in range(messaging.RATE_LIMIT):
        messaging.check_send("k", "hi", now=1000.0)
    with pytest.raises(messaging.Refused, match="rate limit"):
        messaging.check_send("k", "hi", now=1001.0)
    messaging.check_send("other", "hi", now=1001.0)
    messaging.check_send("k", "hi", now=1000.0 + messaging.RATE_WINDOW_SECS + 1)


def test_the_mailbox_holds_messages_until_they_are_read_once() -> None:
    assert messaging.enqueue("k", "m1", "one") == 1
    assert messaging.enqueue("k", "m2", "two") == 2
    assert messaging.waiting("k") == 2
    assert [m["text"] for m in messaging.drain("k")] == ["one", "two"]
    assert messaging.drain("k") == []
    assert messaging.drain("never-written") == []


def test_a_full_mailbox_refuses_and_an_old_message_expires() -> None:
    for i in range(messaging.MAX_QUEUED):
        messaging.enqueue("k", f"m{i}", "x", now=1000.0)
    with pytest.raises(messaging.Refused, match="unread"):
        messaging.enqueue("k", "over", "x", now=1000.0)
    assert messaging.drain("k", now=1000.0 + messaging.MESSAGE_TTL_SECS + 1) == []


def test_a_mailbox_key_cannot_leave_the_mailbox_directory() -> None:
    key = messaging.key({"machine_uuid": "../../etc", "ref": "../x"})
    assert "/" not in key
    messaging.enqueue(key, "m1", "x")
    assert [p.parent for p in messaging.mailbox_dir().iterdir()] == [messaging.mailbox_dir()]




class _Transport:
    is_terminated = False

    def __init__(self, write_stream: Any = None, stream_open: bool = True) -> None:
        self._write_stream = write_stream
        self._request_streams = {GET_STREAM_KEY: object()} if stream_open else {}


def _ctx(sid: str) -> Any:
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(headers={"mcp-session-id": sid})))


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Transport]:
    'daemon.'
    from agent_context import machine
    _local_claim("11112222-aaaa", 1, project="store")
    _local_claim("33334444-bbbb", 2, project="example-api")
    _remote_claim(KEY_A, project="example-api")
    transports = {"s1": _Transport(), "s2": _Transport(), "s3": _Transport()}
    monkeypatch.setattr(S, "_SESSION_OWNERS", {
        "s1": {"pid": 1, "wake": "claude-code"},
        "s2": {"pid": 2, "wake": "claude-code"},
        "s3": {"session_key": KEY_A, "machine_uuid": LAPTOP_UUID, "machine_id": "laptop",
               "wake": None},
    })
    monkeypatch.setattr(S, "_transport", transports.get)
    monkeypatch.setattr(machine, "get_chezmoi_machine_id", lambda: HOST)
    monkeypatch.setattr(machine, "get_machine_uuid", lambda: HOST_UUID)
    return transports


def test_list_agents_lists_every_other_connected_session(daemon: dict[str, _Transport]) -> None:
    rows = S.list_agents(_ctx("s1")).splitlines()
    assert [r.split(" · ")[0] for r in rows] == ["example-api [aaaaaaaa]", "example-api [33334444]"]
    assert rows[0].endswith(" · queued") and not rows[1].endswith(" · queued")
    daemon["s2"]._request_streams.clear()            
    assert "33334444" not in S.list_agents(_ctx("s1"))


def test_a_row_shows_the_turn_the_sessions_bridge_reported(
        daemon: dict[str, _Transport], monkeypatch: pytest.MonkeyPatch) -> None:
    def row() -> str:
        return next(r for r in S.list_agents(_ctx("s1")).splitlines() if "33334444" in r)

    assert not row().endswith(("busy", "idle"))             
    monkeypatch.setattr(S.mcp, "get_context", lambda: _ctx("s2"))    
    for state in ("busy", "idle"):
        assert json.loads(S.relay_report("peer", "", json.dumps(
            {"event": "turn", "state": state}))) == {"turn": state}
        assert row().endswith(f" · {state}")
    assert "error" in json.loads(S.relay_report("peer", "", json.dumps(
        {"event": "turn", "state": "asleep"})))
    assert row().endswith(" · idle")
    
    monkeypatch.setattr(S.mcp, "get_context", lambda: _ctx("s3"))
    S.relay_report("peer", "", json.dumps({"event": "turn", "state": "busy"}))
    assert S.list_agents(_ctx("s1")).splitlines()[0].endswith(" · busy · queued")


def test_a_session_sets_its_own_name_and_is_addressed_by_it(daemon: dict[str, _Transport]) -> None:
    said = S.list_agents(_ctx("s2"), name="  api   fixer ").splitlines()
    assert said[0] == "This session is api fixer [33334444]."
    
    assert "api fixer [33334444]" in S.list_agents(_ctx("s1"))
    answer = json.loads(anyio.run(S.send_message, "api fixer", _ctx("s1"), "hi"))
    assert answer["to"] == "api fixer [33334444]"
    assert "hi" in S.read_notifications(_ctx("s2"))          
    
    assert messaging.names() == {"uuid-ls.33334444": "api fixer"}
    
    assert S.list_agents(_ctx("s2"), name="").splitlines()[0] == (
        "This session is example-api [33334444]. Its name is the derived one again.")
    assert messaging.names() == {}


def test_a_name_that_could_not_be_an_address_is_refused(daemon: dict[str, _Transport]) -> None:
    for bad in ("with [bracket]", "a · b", "x" * 65, "line\nbreak"):
        if bad == "line\nbreak":                    
            assert "This session is line break" in S.list_agents(_ctx("s2"), name=bad)
            continue
        assert "error" in json.loads(S.list_agents(_ctx("s2"), name=bad)), bad


def test_a_caller_the_daemon_cannot_name_is_refused(daemon: dict[str, _Transport]) -> None:
    for answer in (S.list_agents(_ctx("unknown")), S.read_notifications(None),
                   anyio.run(S.send_message, "store", _ctx("unknown"), "hi")):
        assert json.loads(answer)["error"].startswith("no address found")


def test_send_message_pushes_to_a_session_with_a_wake_route_and_stores_nothing(
        daemon: dict[str, _Transport]) -> None:
    async def body() -> tuple[dict, Any]:
        send, recv = anyio.create_memory_object_stream[Any](4)
        daemon["s2"]._write_stream = send
        answer = json.loads(await S.send_message("example-api [33334444]", _ctx("s1"), "tests pass?",
                                                 summary="ask", notify_when_idle=True))
        return answer, recv.receive_nowait()

    answer, pushed = anyio.run(body)
    root = pushed.message.root
    assert root.method == peer_wake.PEER_MESSAGE_METHOD
    assert root.params == {"id": answer["sent"], "sender": "uuid-ls.11112222", "text": (
        '<cross-session-message from="store [11112222]" via="agent-context">\ntests pass?\n</cross-session-message>')}
    assert peer_wake.peer_message(root) == root.params      
    assert answer["to"] == "example-api [33334444]" and "claude-code" in answer["delivery"]
    assert "no idle notice" in answer["notify_when_idle"]    
    assert S.read_notifications(_ctx("s2")) == "No notifications."


def test_send_message_queues_for_a_session_with_no_wake_route_until_it_reads(
        daemon: dict[str, _Transport]) -> None:
    answer = json.loads(anyio.run(S.send_message, "example-api [aaaaaaaa]", _ctx("s1"), "hello"))
    assert answer["waiting"] == 1 and answer["delivery"].startswith("queued")
    assert S.read_notifications(_ctx("s1")) == "No notifications."
    got = S.read_notifications(_ctx("s3"))
    assert got == '<cross-session-message from="store [11112222]" via="agent-context">\nhello\n</cross-session-message>'
    assert S.read_notifications(_ctx("s3")) == "No notifications."
    
    reply = json.loads(anyio.run(S.send_message, "store [11112222]", _ctx("s3"), "hi back"))
    assert reply["to"] == "store [11112222]"


def test_send_message_tells_the_bridge_of_a_session_with_a_hook_that_mail_waits(
        daemon: dict[str, _Transport]) -> None:
    S._SESSION_OWNERS["s3"]["notice"] = True

    async def body() -> tuple[dict, dict, Any]:
        send, recv = anyio.create_memory_object_stream[Any](4)
        daemon["s3"]._write_stream = send
        first = json.loads(await S.send_message("example-api [aaaaaaaa]", _ctx("s1"), "one"))
        second = json.loads(await S.send_message("example-api [aaaaaaaa]", _ctx("s1"), "two"))
        return first, second, [recv.receive_nowait().message.root for _ in range(2)]

    first, second, pushed = anyio.run(body)
    assert [p.method for p in pushed] == [peer_wake.PEER_WAITING_METHOD] * 2
    assert [p.params for p in pushed] == [{"count": 1}, {"count": 2}]
    assert [peer_wake.peer_waiting(p) for p in pushed] == [1, 2]     
    assert "hook" in first["delivery"] and second["waiting"] == 2
    
    assert S.read_notifications(_ctx("s3")).count("<cross-session-message") == 2


def test_a_codex_session_with_no_thread_yet_is_queued_for_and_woken_once_it_has_one(
        daemon: dict[str, _Transport]) -> None:
    S._SESSION_OWNERS["s4"] = {"pid": 4, "wake": "codex"}       
    daemon["s4"] = _Transport()

    async def body() -> tuple[dict, dict, Any]:
        send, recv = anyio.create_memory_object_stream[Any](4)
        daemon["s4"]._write_stream = send
        early = json.loads(await S.send_message("p4", _ctx("s1"), "one"))
        _local_claim("55556666-cccc", 4, project="Codexed")      
        late = json.loads(await S.send_message("Codexed", _ctx("s1"), "two"))
        return early, late, recv.receive_nowait().message.root

    assert any(r.endswith("[p4] · server-host · queued") for r in S.list_agents(_ctx("s1")).splitlines())
    early, late, pushed = anyio.run(body)
    assert early["delivery"].startswith("queued") and "codex" in late["delivery"]
    assert pushed.method == peer_wake.PEER_MESSAGE_METHOD and "two" in pushed.params["text"]


def _streams(daemon: dict[str, _Transport], *sids: str) -> dict[str, Any]:
    out = {}
    for sid in sids:
        send, recv = anyio.create_memory_object_stream[Any](8)
        daemon[sid]._write_stream = send
        out[sid] = recv
    return out


def _drain(recv: Any) -> list[Any]:
    got = []
    while True:
        try:
            got.append(recv.receive_nowait().message.root)
        except anyio.WouldBlock:
            return got




def test_a_channel_name_is_told_from_a_sessions_address() -> None:
    assert messaging.channel_name("example-api [33334444]") is None
    assert messaging.channel_name(" #  release   team ") == "release team"
    assert messaging.channel_arg("team") == messaging.channel_arg("#team") == "team"
    for bad in ("#", "#a#b", "#with [bracket]", "#a · b", "#" + "x" * 65):
        with pytest.raises(messaging.Refused):
            messaging.channel_name(bad)


def test_a_post_reaches_every_other_session_of_a_channel_held_by_rule(
        daemon: dict[str, _Transport]) -> None:
    async def body(to: str) -> tuple[dict, dict[str, list[Any]]]:
        recv = _streams(daemon, "s1", "s2")
        sent = json.loads(await S.send_message(to, _ctx("s1"), "freeze at noon",
                                               notify_when_idle=True))
        return sent, {sid: _drain(r) for sid, r in recv.items()}

    
    sent, pushed = anyio.run(body, "#mobileapi")
    assert sent["to"] == "#mobileapi"
    assert sent["delivery"].startswith("1 session(s) handed to a wake route, 1 queued")
    assert "no idle notice" in sent["notify_when_idle"]
    [woke] = pushed["s2"]
    assert 'from="store [11112222]" via="agent-context" channel="#mobileapi">' in woke.params["text"]
    assert "freeze at noon" in S.read_notifications(_ctx("s3"))     
    assert pushed["s1"] == []                                       
    
    sent, pushed = anyio.run(body, "#laptop")
    assert sent["delivery"].startswith("0 session(s) handed to a wake route, 1 queued")
    assert pushed["s2"] == []
    sent, pushed = anyio.run(body, "#all")
    assert sent["delivery"].startswith("1 session(s) handed to a wake route, 1 queued")
    assert "error" in json.loads(anyio.run(S.send_message, "#a#b", _ctx("s1"), "x"))
    assert "error" in json.loads(anyio.run(S.send_message, "#all", _ctx("s1"), " "))


def test_a_session_joins_a_channel_reads_its_history_and_leaves(
        daemon: dict[str, _Transport]) -> None:
    
    first = json.loads(anyio.run(S.send_message, "#release", _ctx("s1"), "plan is in the doc"))
    assert first["delivery"].startswith("0 session(s) handed to a wake route, 0 queued")
    said = S.list_agents(_ctx("s3"), join="release").splitlines()
    assert said[0] == "This session joined #release."
    assert said[1] == "#release: 0 other live session(s)."
    assert said[2] == "#release, its 1 post(s) of the past day:"
    assert "plan is in the doc" in "\n".join(said[3:])
    S.list_agents(_ctx("s2"), join="#Release")                  
    assert S.list_agents(_ctx("s1")).splitlines()[-1] == "Joined channels: #release (2)"
    rows = S.list_agents(_ctx("s1"), channel="release").splitlines()
    assert rows[0] == "#release: 2 other live session(s)."
    assert sorted(r.split(" · ")[0] for r in rows[1:3]) == ["example-api [33334444]",
                                                             "example-api [aaaaaaaa]"]
    second = json.loads(anyio.run(S.send_message, "#release", _ctx("s1"), "shipping"))
    assert second["delivery"].startswith("0 session(s) handed to a wake route, 2 queued")
    assert "shipping" in S.read_notifications(_ctx("s2"))
    
    assert set(json.loads(messaging.channels_path().read_text())["release"]["members"]) == {
        "uuid-ls.33334444", "uuid-laptop.aaaaaaaa"}
    assert S.list_agents(_ctx("s2"), leave="release").splitlines()[0] == "This session left #release."
    assert S.list_agents(_ctx("s2"), leave="release").splitlines()[0] == (
        "This session had not joined #release.")
    third = json.loads(anyio.run(S.send_message, "#release", _ctx("s1"), "done"))
    assert third["delivery"].startswith("0 session(s) handed to a wake route, 1 queued")
    
    assert "It is still in #example-api" in S.list_agents(_ctx("s2"), leave="example-api")


def test_a_session_with_no_claim_is_in_the_channel_of_its_directory() -> None:
    bare = {"machine": "laptop", "ref": "aaaa", "connected": True, "project": None,
            "cwd": "/Users/user/Developer/Personal/example-project/"}
    claimed = {"machine": "laptop", "ref": "bbbb", "connected": True, "project": "Helper",
               "cwd": "/Users/user/Developer/Personal/example-project"}
    assert messaging.members([bare, claimed], "agentorchestra") == [bare]   
    assert messaging.members([bare, claimed], "helper") == [claimed]
    assert messaging.members([{**bare, "cwd": None}], "agentorchestra") == []
    
    sub = {**claimed, "cwd": "/Users/user/Developer/Personal/example-project/Helper"}
    known = frozenset({"agentorchestra", "helper"})
    assert messaging.members([sub], "example-project", known) == [sub]
    assert messaging.members([sub], "helper", known) == [sub]
    assert messaging.members([sub], "example-project") == []          
    assert messaging.members([sub], "Personal", known) == []         


def test_every_bridge_of_one_claude_code_session_has_one_id(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(peer_wake.BRIDGE_ENV, raising=False)
    monkeypatch.setenv(peer_wake.SESSION_ENV, "0a1b2c3d-1111-2222-3333-444455556666")
    first = peer_wake.bridge_id()
    assert S._BRIDGE_KEY.fullmatch(first) and "0a1b2c3d" not in first    
    monkeypatch.delenv(peer_wake.BRIDGE_ENV)            
    assert peer_wake.bridge_id() == first
    monkeypatch.delenv(peer_wake.BRIDGE_ENV)
    monkeypatch.setenv(peer_wake.SESSION_ENV, "another-session")
    assert peer_wake.bridge_id() != first
    monkeypatch.delenv(peer_wake.BRIDGE_ENV)
    monkeypatch.delenv(peer_wake.SESSION_ENV)           
    made = peer_wake.bridge_id()
    assert S._BRIDGE_KEY.fullmatch(made) and peer_wake.bridge_id() == made   


def test_history_is_a_day_and_fifty_posts_and_a_gone_member_is_dropped() -> None:
    for i in range(messaging.MAX_HISTORY + 5):
        messaging.record("Team", f"m{i}", f"post {i}", now=1000.0 + i)
    posts = messaging.history("team", now=2000.0)
    assert len(posts) == messaging.MAX_HISTORY and posts[0]["text"] == "post 5"
    assert messaging.history("team", now=1000.0 + messaging.HISTORY_TTL_SECS + 100) == []
    assert messaging.history("other") == []
    here = {"machine_uuid": "u", "ref": "aaaa", "connected": True}
    gone = {"machine_uuid": "u", "ref": "bbbb", "connected": False}
    messaging.join("team", gone, [gone], now=1000.0)
    messaging.join("team", here, [here, gone], now=1000.0 + messaging.MEMBER_TTL_SECS + 1)
    assert set(json.loads(messaging.channels_path().read_text())["team"]["members"]) == {"u.aaaa"}
    assert messaging.leave("team", here, [here])
    assert messaging.channels_path().read_text() == "{}"        


def test_a_wake_that_failed_is_queued_and_the_sender_is_told(
        daemon: dict[str, _Transport], monkeypatch: pytest.MonkeyPatch) -> None:
    S._SESSION_OWNERS["s2"]["notice"] = True
    monkeypatch.setattr(S.mcp, "get_context", lambda: _ctx("s2"))       

    async def body() -> tuple[dict, dict, dict[str, list[Any]]]:
        recv = _streams(daemon, "s1", "s2")
        sent = json.loads(await S.send_message("example-api [33334444]", _ctx("s1"), "tests pass?"))
        pushed = _drain(recv["s2"])[0].params
        report = json.loads(S.relay_report("peer", "", json.dumps(
            {"event": "undelivered", "id": pushed["id"], "text": pushed["text"],
             "sender": pushed["sender"]})))
        await anyio.sleep(0.05)
        return sent, report, {sid: _drain(r) for sid, r in recv.items()}

    sent, report, pushed = anyio.run(body)
    assert report == {"queued": sent["sent"]}
    
    assert [(p.method, p.params) for p in pushed["s2"]] == [
        (peer_wake.PEER_WAITING_METHOD, {"count": 1})]
    assert "tests pass?" in S.read_notifications(_ctx("s2"))
    
    [notice] = pushed["s1"]
    assert notice.method == peer_wake.PEER_MESSAGE_METHOD
    assert 'from="agent-context"' in notice.params["text"]
    assert f"your message {sent['sent']} to example-api [33334444] could not wake" in notice.params["text"]


def test_a_report_the_daemon_cannot_act_on_is_an_error(
        daemon: dict[str, _Transport], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(S.mcp, "get_context", lambda: _ctx("unknown"))
    for body in ("not json", "[]", json.dumps({"event": "nope"}),
                 json.dumps({"event": "undelivered", "text": "x"}),        
                 json.dumps({"event": "idle", "ref": "deadbeef"})):
        assert "error" in json.loads(S.relay_report("peer", "", body)), body
    monkeypatch.setattr(S.mcp, "get_context", lambda: _ctx("s2"))
    assert "error" in json.loads(S.relay_report("peer", "", json.dumps({"event": "undelivered"})))


def test_notify_when_idle_tells_the_sender_once_when_the_session_ends_its_turn(
        daemon: dict[str, _Transport], monkeypatch: pytest.MonkeyPatch) -> None:
    S._SESSION_OWNERS["s2"]["notice"] = True

    async def body() -> tuple[dict, dict, dict, dict[str, list[Any]]]:
        recv = _streams(daemon, "s1", "s2")
        sent = json.loads(await S.send_message("example-api [33334444]", _ctx("s1"), "ping",
                                               notify_when_idle=True))
        idle = {"event": "idle", "ref": "33334444"}                    
        first = json.loads(S.relay_report("peer", "", json.dumps(idle)))
        second = json.loads(S.relay_report("peer", "", json.dumps(idle)))
        await anyio.sleep(0.05)
        return sent, first, second, {sid: _drain(r) for sid, r in recv.items()}

    sent, first, second, pushed = anyio.run(body)
    assert "one notice arrives" in sent["notify_when_idle"]
    assert [p.method for p in pushed["s2"]] == [peer_wake.PEER_MESSAGE_METHOD,
                                                peer_wake.PEER_WATCH_METHOD]
    assert (first, second) == ({"told": 1}, {"told": 0})                
    [notice] = pushed["s1"]
    assert f"Idle notice: example-api [33334444] ended its turn after your message {sent['sent']}" \
        in notice.params["text"]


def test_an_idle_request_is_kept_on_disk_so_a_daemon_restart_does_not_drop_it() -> None:
    messaging.watch_idle("host.33334444", "host.11112222", "m1", now=1000.0)
    messaging.watch_idle("host.33334444", "laptop.aaaaaaaa", "m2", now=1001.0)
    
    assert json.loads(messaging.watches_path().read_text())["host.33334444"][0]["id"] == "m1"
    assert messaging.take_watchers("host.other", now=1002.0) == []
    got = messaging.take_watchers("host.33334444", now=1002.0)
    assert [(w["sender"], w["id"]) for w in got] == [("host.11112222", "m1"),
                                                     ("laptop.aaaaaaaa", "m2")]
    assert messaging.take_watchers("host.33334444", now=1003.0) == []          
    messaging.watch_idle("host.33334444", "host.11112222", "m3", now=1000.0)
    assert messaging.take_watchers("host.33334444",
                                   now=1000.0 + messaging.WATCH_TTL_SECS + 1) == []   
    messaging.watches_path().write_text("not json")
    assert messaging.take_watchers("host.33334444") == []


def test_a_relay_sessions_idle_report_names_it_by_its_bridge(
        daemon: dict[str, _Transport], monkeypatch: pytest.MonkeyPatch) -> None:
    
    
    S._SESSION_OWNERS["s3"]["notice"] = True

    async def body() -> tuple[dict, dict, dict, list[Any]]:
        recv = _streams(daemon, "s1", "s3")
        await S.send_message("example-api [aaaaaaaa]", _ctx("s1"), "ping", notify_when_idle=True)
        hook_ref = {"event": "idle", "ref": "0f0f0f0f"}
        miss = json.loads(S.relay_report("peer", "", json.dumps(hook_ref)))
        bad = json.loads(S.relay_report("peer", "", json.dumps({**hook_ref, "bridge": "aaaaaaaa"})))
        hit = json.loads(S.relay_report("peer", "", json.dumps({**hook_ref, "bridge": KEY_A})))
        await anyio.sleep(0.05)
        return miss, bad, hit, _drain(recv["s1"])

    miss, bad, hit, pushed = anyio.run(body)
    assert "error" in miss and "error" in bad and hit == {"told": 1}
    assert "Idle notice: example-api [aaaaaaaa] ended its turn" in pushed[0].params["text"]


def test_send_message_queues_when_the_push_is_not_taken(daemon: dict[str, _Transport]) -> None:
    answer = json.loads(anyio.run(S.send_message, "example-api [33334444]", _ctx("s1"), "hello"))
    assert answer["delivery"].startswith("queued")      
    assert "hello" in S.read_notifications(_ctx("s2"))


def test_send_message_refuses_with_a_reason(daemon: dict[str, _Transport]) -> None:
    def send(to: str, message: str) -> str:
        return json.loads(anyio.run(S.send_message, to, _ctx("s1"), message))["error"]

    assert "2 live sessions" in send("example-api", "hi")
    assert "no live session found" in send("nobody", "hi")
    assert "empty" in send("example-api [33334444]", "  ")
    assert "cap" in send("example-api [33334444]", "x" * (messaging.MAX_MESSAGE_CHARS + 1))
    daemon["s2"]._request_streams.clear()
    assert "no live session found" in send("example-api [33334444]", "hi")




def _open(who: Identity | None, headers: list[tuple[bytes, bytes]], sid: str) -> None:
    async def body() -> None:
        async def send(message: dict) -> None:
            return None
        wrapped = S._remember_owner(send, who, headers)
        await wrapped({"type": "http.response.start", "status": 200,
                       "headers": [(b"mcp-session-id", sid.encode())]})
    anyio.run(body)


def test_initialize_records_the_owner_and_the_route_its_bridge_declared(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(S, "_SESSION_OWNERS", {})
    monkeypatch.setattr(S, "caller_pid", lambda: 4242)
    _open(None, [(b"x-bridge-wake", b"claude-code")], "s1")
    who = Identity(machine_uuid=LAPTOP_UUID, machine_id="laptop", platform=None, home=None,
                   session_key=KEY_A)
    _open(who, [(b"X-Bridge-Wake", b"not a route!")], "s2")
    _open(None, [(b"x-bridge-notice", b"hook")], "s3")
    _open(None, [(b"x-bridge-notice", b"something else")], "s4")
    assert S._SESSION_OWNERS == {
        "s1": {"pid": 4242, "wake": "claude-code"},
        "s2": {"session_key": KEY_A, "machine_uuid": LAPTOP_UUID, "machine_id": "laptop",
               "wake": None},
        "s3": {"pid": 4242, "wake": None, "notice": True},
        "s4": {"pid": 4242, "wake": None},
    }


def test_a_helper_connection_names_its_machine_and_is_no_agent(
        monkeypatch: pytest.MonkeyPatch) -> None:
    
    monkeypatch.setattr(S, "_SESSION_OWNERS", {})
    helper = Identity(machine_uuid=LAPTOP_UUID, machine_id="laptop", platform=None, home=None,
                      session_key="0" * 32, helper=True)
    _open(helper, [], "h1")
    assert S._SESSION_OWNERS == {}
    assert claims.ensure_remote_claim(helper) is not None       


def test_a_bridge_leaves_its_identity_headers_for_the_machines_scripts() -> None:
    from agent_context import identity, paths
    path = paths.state_dir() / identity.IDENTITY_FILE
    identity.publish_local_headers({"x-agent-context-machine": LAPTOP_UUID,
                                    "x-agent-context-machine-id": "laptop"})
    assert json.loads(path.read_text()) == {"x-agent-context-machine": LAPTOP_UUID,
                                            "x-agent-context-machine-id": "laptop"}
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    stamp = path.stat().st_mtime_ns
    identity.publish_local_headers({"x-agent-context-machine-id": "laptop",
                                    "x-agent-context-machine": LAPTOP_UUID})
    assert path.stat().st_mtime_ns == stamp                      


def test_a_bridge_id_is_the_session_key_only_when_well_formed() -> None:
    assert S._bridge_key([(b"x-bridge-id", KEY_A.encode())]) == KEY_A
    assert S._bridge_key([(b"x-bridge-id", b"../../claims/x")]) is None
    assert S._bridge_key([]) is None


def test_a_bridge_declares_its_id_and_its_route_on_every_connect(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(peer_wake.BRIDGE_ENV, raising=False)
    monkeypatch.delenv(peer_wake.SOCKET_ENV, raising=False)
    first = peer_wake.connect_headers()
    assert S._bridge_key([(k.encode(), v.encode()) for k, v in first.items()]) == first["x-bridge-id"]
    assert "x-bridge-wake" not in first
    assert first["x-bridge-notice"] == "hook"      
    monkeypatch.setenv(peer_wake.SOCKET_ENV, "/run/s.sock")
    monkeypatch.setenv(peer_wake.TOKEN_ENV, "t")
    assert peer_wake.connect_headers() == {"x-bridge-id": first["x-bridge-id"],
                                           "x-bridge-wake": "claude-code",
                                           "x-bridge-notice": "hook",
                                           "x-bridge-cwd": first["x-bridge-cwd"]}


def test_a_session_with_no_claim_is_named_by_the_directory_its_bridge_declared(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    spaced = tmp_path / "my project"
    spaced.mkdir()
    monkeypatch.chdir(spaced)
    sent = peer_wake.connect_headers()["x-bridge-cwd"]
    assert " " not in sent and peer_wake.declared_cwd(sent) == str(spaced)
    for bad in ("", "relative/dir", "/a%0Ab", "/" + "a" * 2000):
        assert peer_wake.declared_cwd(bad) is None
    assert peer_wake.declared_cwd("C%3A%5CUsers%5Cuser") == "C:\\Users\\user"

    monkeypatch.setattr(S, "_SESSION_OWNERS", {})
    who = Identity(machine_uuid=LAPTOP_UUID, machine_id="laptop", platform=None, home=None,
                   session_key=KEY_A)
    _open(who, [(b"x-bridge-cwd", sent.encode())], "s1")
    assert S._SESSION_OWNERS["s1"]["cwd"] == str(spaced)
    got = _roster([{**S._SESSION_OWNERS["s1"], "sid": "s1", "connected": True}])
    assert (got[0]["name"], got[0]["cwd"]) == ("my project", str(spaced))
    
    _remote_claim(KEY_A, project="example-project", cwd="/Users/user/ao")
    got = _roster([{**S._SESSION_OWNERS["s1"], "sid": "s1", "connected": True}])
    assert (got[0]["name"], got[0]["cwd"]) == ("example-project", "/Users/user/ao")
