"policy/policy: the release a daemon serves is the code it booted with.\n\nThe snapshot is taken once; a tree that changes after it (a landing whose gate has not run yet)\ndoes not change what relays install, and every response names the snapshot's ETag."
from pathlib import Path

import anyio

from agent_context import relay_source, server


def _tree(root: Path, text: str) -> Path:
    server_dir = root / "server"
    (server_dir / "src" / "agent_context").mkdir(parents=True, exist_ok=True)
    (server_dir / "pyproject.toml").write_text("[project]\nname = 'agent-context'\n")
    (server_dir / "src" / "agent_context" / "__init__.py").write_text(text)
    return server_dir


def test_the_snapshot_does_not_follow_the_tree(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(relay_source, "_RELEASES", {})
    server_dir = _tree(tmp_path, "one = 1\n")
    etag, body = relay_source.snapshot_release(server_dir)
    _tree(tmp_path, "two = 2\n")
    assert relay_source.snapshot_release(server_dir) == (etag, body)
    assert relay_source.current_etag(server_dir) == etag
    assert relay_source.served_etag() == etag
    assert relay_source.etag_of(relay_source.build_relay_source(server_dir)) != etag


def test_without_a_snapshot_the_current_etag_is_the_tree(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(relay_source, "_RELEASES", {})
    server_dir = _tree(tmp_path, "one = 1\n")
    assert relay_source.served_etag() is None
    assert relay_source.current_etag(server_dir) == relay_source.etag_of(
        relay_source.build_relay_source(server_dir))


def test_every_response_names_the_release(monkeypatch) -> None:
    monkeypatch.setattr(relay_source, "_RELEASES", {Path("/x"): ('"rel"', b"")})
    monkeypatch.setattr(server, "served_etag", relay_source.served_etag)
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def main() -> None:
        stamped = server._with_release(send)
        await stamped({"type": "http.response.start", "status": 200,
                       "headers": [(b"content-type", b"text/plain")]})
        await stamped({"type": "http.response.body", "body": b"x"})

    anyio.run(main)
    assert (relay_source.RELEASE_HEADER.lower().encode(), b'"rel"') in sent[0]["headers"]
    assert (b"content-type", b"text/plain") in sent[0]["headers"]
    assert sent[1] == {"type": "http.response.body", "body": b"x"}


def test_no_snapshot_adds_no_header(monkeypatch) -> None:
    monkeypatch.setattr(relay_source, "_RELEASES", {})

    async def send(message: dict) -> None:
        pass

    assert server._with_release(send) is send
