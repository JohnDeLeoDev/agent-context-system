'Edges of the machine-identity work that test_machine_identity.py does not pin.'
from __future__ import annotations

import json

import pytest

from agent_context import claims, fleet, identity, project_resolve
from agent_context import fstools as T

LAPTOP = identity.Identity(machine_uuid="AAAA-LAPTOP", machine_id="laptop",
                           platform="darwin", home="/Users/user", session_key="key-1")


@pytest.fixture
def as_laptop():
    token = identity.bind(LAPTOP)
    yield LAPTOP
    identity.reset(token)


def test_register_path_from_a_remote_caller_is_an_explicit_error(
        store, as_laptop: identity.Identity, tmp_path) -> None:
    r = T.machine_admin(store, "register_path", cwd=str(tmp_path))
    assert "error" in r and "daemon host" in r["error"]
    assert not (tmp_path / ".agents").exists()


def test_a_remote_claim_is_not_published_as_a_session_of_the_daemon_host(as_laptop) -> None:
    claims.ensure_remote_claim(LAPTOP, cwd="/Users/user/x")
    assert claims.read_live() == []
    assert [c["cwd"] for c in claims.read_remote()] == ["/Users/user/x"]


def test_an_expired_remote_claim_is_dropped_and_its_file_removed(as_laptop) -> None:
    claims.ensure_remote_claim(LAPTOP, cwd="/x", now=1000.0)
    assert claims.read_remote(LAPTOP.machine_uuid, now=1000.0 + claims.TTL_SECS + 1) == []
    assert list(claims.claims_dir().glob("remote-*.json")) == []


def test_a_claim_without_a_session_key_is_not_recorded() -> None:
    bare = identity.Identity("AAAA-LAPTOP", "laptop", "darwin", "/Users/user")
    assert claims.ensure_remote_claim(bare, cwd="/x") is None


def test_the_fleet_read_path_includes_remote_claims(store, as_laptop) -> None:
    claims.ensure_remote_claim(LAPTOP, cwd="/Users/user/x")
    rows = {r["machine_uuid"]: r for r in fleet.read_all(store.root)}
    assert [s["cwd"] for s in rows[LAPTOP.machine_uuid]["sessions"]] == ["/Users/user/x"]


def test_merging_does_not_mutate_its_input_rows() -> None:
    rows = [{"machine_uuid": "u", "sessions": []}]
    fleet.merge_remote_claims(rows, [{"machine_uuid": "u", "session": "s", "last_seen": 1}])
    assert rows == [{"machine_uuid": "u", "sessions": []}]


def test_an_https_remote_normalizes_like_its_ssh_spelling() -> None:
    assert (project_resolve.normalize_remote("https://github.com/acme/mobile-api.git")
            == project_resolve.normalize_remote("git@github.com:acme/mobile-api.git")
            == "github.com:acme/mobile-api")


def test_the_refusal_is_json_serializable(store) -> None:
    ghost = identity.Identity("NOPE", "ghost", "linux", "/x")
    assert json.dumps(identity.refusal(store, ghost))
