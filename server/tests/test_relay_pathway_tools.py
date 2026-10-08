"get_materialized, relay_report: the MCP tools that replaced the relay's GET /materialized,\nPUT /token-usage/... and PUT /deps/... routes, removed now that every daemon and relay uses\nMCP (policy/policy)."
from __future__ import annotations

import json

import pytest

from agent_context import server

KNOWN_UUID = "10000000-0000-4000-8000-000000000001"
HOME = "/Users/known/agent"
HOSTNAME = "known-machine"
USAGE_BLOCK = {"columns": ["day", "requests"], "rows": [["2026-09-01", 3]]}
FLEET_OK = {"ok": True, "missing": [], "broken": [], "below_floor": []}


@pytest.fixture
def srv(store, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_get_conn", lambda: store)
    return server


@pytest.fixture
def machine(store):
    import os

    path = os.path.join(store.root, "machines", KNOWN_UUID + ".toml")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(f'type = "machine"\nmachine_uuid = "{KNOWN_UUID}"\nhostname = "{HOSTNAME}"\n'
                 f'home_dir = "{HOME}"\ndisplay_name = "known"\nplatform = "linux"\n')
    store.reload()
    return KNOWN_UUID


def _call(tool: str, **kw) -> dict:
    return json.loads(getattr(server, tool)(**kw))




def test_get_materialized_matches_the_route_for_no_evidence(srv, tmp_path) -> None:
    from agent_context.materialize import build_materialized_map
    (tmp_path).mkdir(exist_ok=True)
    out = _call("get_materialized")
    assert "error" not in out, out
    assert out == build_materialized_map(srv._get_conn().root)


def test_get_materialized_refuses_too_many_evidence_values(srv) -> None:
    out = _call("get_materialized", remote=[f"git@h:o/r{i}.git" for i in range(9)])
    assert "error" in out


def test_get_materialized_unknown_evidence_selects_nothing(srv) -> None:
    from agent_context.materialize import build_materialized_map
    out = _call("get_materialized", project=["00000000-0000-0000-0000-000000000000"])
    assert out == build_materialized_map(srv._get_conn().root)




def test_relay_report_token_usage_writes_under_the_resolved_machine(srv, machine) -> None:
    body = json.dumps({"hostname": HOSTNAME, "home_dir": HOME, "usage": USAGE_BLOCK})
    out = _call("relay_report", kind="token_usage",
               uuid_hint="99999999-0000-4000-8000-000000000099", month="2026-09", body=body)
    assert out == {"machine_uuid": machine, "written": True}, out
    written = srv._get_conn().root
    import os
    path = os.path.join(written, "machines", machine, "token-usage", "2026-09.json")
    assert json.loads(open(path).read())["usage"] == USAGE_BLOCK


def test_relay_report_token_usage_keeps_the_tools_block(srv, machine) -> None:
    tools = {"columns": ["day", "tool", "calls"], "rows": [["2026-09-01", "Bash", 4]]}
    body = json.dumps({"hostname": HOSTNAME, "home_dir": HOME, "usage": USAGE_BLOCK,
                       "tools": tools})
    out = _call("relay_report", kind="token_usage",
               uuid_hint="99999999-0000-4000-8000-000000000099", month="2026-09", body=body)
    assert out == {"machine_uuid": machine, "written": True}, out
    import os
    path = os.path.join(srv._get_conn().root, "machines", machine, "token-usage", "2026-09.json")
    assert json.loads(open(path).read()) == {"usage": USAGE_BLOCK, "tools": tools}


def test_relay_report_token_usage_refuses_a_tools_block_that_is_not_an_object(srv, machine) -> None:
    body = json.dumps({"hostname": HOSTNAME, "home_dir": HOME, "usage": USAGE_BLOCK,
                       "tools": ["Bash"]})
    out = _call("relay_report", kind="token_usage",
               uuid_hint="99999999-0000-4000-8000-000000000099", month="2026-09", body=body)
    assert out == {"error": "tools must be a JSON object", "status": 400}


def test_relay_report_token_usage_no_match_is_an_error_with_status(srv) -> None:
    body = json.dumps({"hostname": "ghost", "home_dir": "/nowhere", "usage": USAGE_BLOCK})
    out = _call("relay_report", kind="token_usage",
               uuid_hint="99999999-0000-4000-8000-000000000099", month="2026-09", body=body)
    assert out == {"error": "no known machine matches hostname/home_dir", "status": 404}


def test_relay_report_token_usage_bad_month_is_refused(srv, machine) -> None:
    body = json.dumps({"hostname": HOSTNAME, "home_dir": HOME, "usage": USAGE_BLOCK})
    out = _call("relay_report", kind="token_usage",
               uuid_hint="99999999-0000-4000-8000-000000000099", month="2026-13", body=body)
    assert out["error"] == "month must be YYYY-MM" and out["status"] == 400


def test_relay_report_token_usage_missing_month_is_refused(srv, machine) -> None:
    body = json.dumps({"hostname": HOSTNAME, "home_dir": HOME, "usage": USAGE_BLOCK})
    out = _call("relay_report", kind="token_usage",
               uuid_hint="99999999-0000-4000-8000-000000000099", body=body)
    assert out["status"] == 400 and "month" in out["error"]


def test_relay_report_token_usage_oversized_body_is_refused(srv, machine) -> None:
    huge = {"columns": ["x"], "rows": [["y" * 6_000_000]]}
    body = json.dumps({"hostname": HOSTNAME, "home_dir": HOME, "usage": huge})
    out = _call("relay_report", kind="token_usage",
               uuid_hint="99999999-0000-4000-8000-000000000099", month="2026-09", body=body)
    assert out == {"error": "body too large", "status": 413}




def test_relay_report_deps_writes_under_the_resolved_machine(srv, machine) -> None:
    body = json.dumps({"hostname": HOSTNAME, "home_dir": HOME, "at": 1234, "fleet": FLEET_OK})
    out = _call("relay_report", kind="deps", uuid_hint="99999999-0000-4000-8000-000000000099",
               body=body)
    assert out == {"machine_uuid": machine, "written": True}, out
    import os
    path = os.path.join(srv._get_conn().root, "machines", machine, "deps.json")
    assert json.loads(open(path).read())["fleet"] == FLEET_OK


def test_relay_report_deps_no_match_is_an_error_with_status(srv) -> None:
    body = json.dumps({"hostname": "ghost", "home_dir": "/nowhere", "at": 1, "fleet": FLEET_OK})
    out = _call("relay_report", kind="deps", uuid_hint="99999999-0000-4000-8000-000000000099",
               body=body)
    assert out == {"error": "no known machine matches hostname/home_dir", "status": 404}


def test_relay_report_deps_malformed_body_is_refused(srv, machine) -> None:
    out = _call("relay_report", kind="deps",
               uuid_hint="99999999-0000-4000-8000-000000000099", body="not json")
    assert out["status"] == 400


def test_relay_report_deps_refuses_a_month(srv, machine) -> None:
    body = json.dumps({"hostname": HOSTNAME, "home_dir": HOME, "at": 1, "fleet": FLEET_OK})
    out = _call("relay_report", kind="deps", uuid_hint="99999999-0000-4000-8000-000000000099",
               month="2026-09", body=body)
    assert out["status"] == 400 and "month" in out["error"]
