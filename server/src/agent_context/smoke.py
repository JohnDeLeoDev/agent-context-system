'Smoke test of every agent-context MCP tool: `python -m agent_context.smoke`.\n\nRead tools run against the live daemon (over the network, as a relay would). Write tools run\nagainst a throwaway store in this process, never the live one. Prints one line per tool and\nexits 1 on any failure. The tables must name every tool the server registers: a test fails\nwhen a tool is added without an entry here.\n\nThe token for --url is read from AGENT_CONTEXT_TOKEN.'
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

Call = Callable[..., Any]



SEED_MEMORY = "smoke-memory"
SEED_DOC = "smoke/doc.md"

READ_TOOLS: dict[str, dict[str, Any]] = {
    "check_integrity": {},
    "explore": {"kind": "memory", "key": SEED_MEMORY},
    "get_doc": {"path": SEED_DOC},
    "get_entity": {"kind": "memory", "key": SEED_MEMORY},
    "get_health": {},
    "get_materialized": {},
    "get_instructions": {},
    "get_memory": {"slug": SEED_MEMORY},
    "get_session_context": {"cwd": "/"},
    "get_usage_report": {},
    "get_version_history": {"entity_type": "memory", "key": SEED_MEMORY},
    "list_audit_observations": {"status": "open"},
    "list_entities": {"kind": "memory", "limit": 5},
    "list_agents": {},
    "list_machines": {},
    "read_notifications": {},
    "resolve_project": {"cwd": "/"},
    "search_all": {"query": "smoke", "limit": 3},
}


_NOT_FOUND = re.compile(r"^no [\w ]+ found\b", re.IGNORECASE)

_NEEDS_CONTEXT = {"get_session_context", "resolve_project", "list_agents", "read_notifications"}


def invoke(fn: Callable[..., Any], **kw: Any) -> Any:
    'Call one tool function and decode its reply; an async tool (run_store_task) is run to\n    completion here, since the battery itself is synchronous.'
    result = fn(**kw)
    if inspect.iscoroutine(result):
        result = asyncio.run(result)
    return decode(result)


def decode(text: str) -> Any:
    "A tool's answer: JSON for most tools, markdown for get_session_context."
    try:
        return json.loads(text)
    except ValueError:
        return text


def _failure(result: Any) -> str | None:
    if isinstance(result, dict) and "error" in result and not _NOT_FOUND.search(str(result["error"])):
        return str(result["error"])
    return None


def _step(call: Call, tool: str, **kw: Any) -> Any:
    'One tool call in a write sequence. Any error answer fails the sequence: a write that\n    cannot find what it just created is a defect, not an empty result.'
    result = call(tool, **kw)
    if isinstance(result, dict) and "error" in result:
        raise AssertionError(f"{tool}: {result['error']}")
    return result


def _expect_memory(call: Call, slug: str, field: str, contains: str) -> None:
    'The write really landed: read the memory back and check one field.'
    got = call("get_memory", slug=slug)
    if not isinstance(got, dict) or contains not in str(got.get(field)):
        raise AssertionError(f"get_memory {slug!r}: {field} does not contain {contains!r}: {got!r}")


def _memory(call: Call, slug: str, body: str = "b") -> None:
    _step(call, "upsert_memory", slug=slug, memory_type="reference", description=f"about {slug}",
          body=body, load_behavior="lazy")


def _doc(call: Call, path: str = SEED_DOC) -> None:
    _step(call, "upsert_doc", path=path, body="hello", title="Smoke doc")


def _observation(call: Call) -> int:
    made = _step(call, "add_audit_observation", observation="smoke observation, safe to delete",
                 scope="universal", evidence="written by agent_context.smoke")
    return int(made["id"])


def _w_upsert_memory(call: Call) -> None:
    _memory(call, SEED_MEMORY)
    _w_dry_run(call)
    _w_set_description(call)
    _w_set_load_behavior(call)


def _w_set_description(call: Call) -> None:
    _memory(call, "smoke-desc")
    _step(call, "upsert_memory", slug="smoke-desc", description="a new description")
    _expect_memory(call, "smoke-desc", "description", "a new description")


def _w_set_load_behavior(call: Call) -> None:
    _memory(call, "smoke-load")            
    _step(call, "upsert_memory", slug="smoke-load", load_behavior="always")
    _expect_memory(call, "smoke-load", "load_behavior", "always")


def _w_upsert_doc(call: Call) -> None:
    _doc(call)
    _doc(call, "smoke/append.md")
    _step(call, "upsert_doc", path="smoke/append.md", body="more", append=True)


def _w_register_path(call: Call) -> None:
    
    err = _failure(call("register_path", cwd="/"))
    if err is not None:
        raise AssertionError(f"register_path: {err}")


def _w_update_observation(call: Call) -> None:
    _step(call, "update_audit_observation", observation_id=_observation(call), note="a note")


def _w_resolve_observation(call: Call) -> None:
    _step(call, "resolve_audit_observation", observation_id=_observation(call),
          resolution_note="done")


def _w_delete_entity(call: Call) -> None:
    _memory(call, "smoke-delete")
    _step(call, "delete_entity", kind="memory", key="smoke-delete")
    if call("get_memory", slug="smoke-delete") is not None:
        raise AssertionError("delete_entity: the memory is still there")


def _w_edit_body(call: Call) -> None:
    _memory(call, "smoke-edit", "body one")
    _step(call, "edit_body", kind="memory", key="smoke-edit", old_string="body one",
          new_string="body two")
    _expect_memory(call, "smoke-edit", "body", "body two")


def _w_set_entity_links(call: Call) -> None:
    _memory(call, "smoke-link-a")
    _memory(call, "smoke-link-b")
    _step(call, "set_entity_links", kind="memory", key="smoke-link-a",
          links={"sibling": ["smoke-link-b"]})


def _w_bulk_edit(call: Call) -> None:
    _memory(call, "smoke-bulk", "body one")
    done = _step(call, "bulk_edit", edits=[{"kind": "memory", "key": "smoke-bulk",
                                            "replacements": [["body one", "body two"]]}])
    failed = [r for r in done.get("results", []) if r.get("error")]
    if failed:
        raise AssertionError(f"bulk_edit: {failed[0]['error']}")
    _expect_memory(call, "smoke-bulk", "body", "body two")


def _w_dry_run(call: Call) -> None:
    _memory(call, "smoke-dry-target")
    out = _step(call, "upsert_memory", slug="smoke-dry-new", memory_type="reference",
                description="d", body="b", load_behavior="lazy", dry_run=True)
    if not out.get("dry_run") or not out.get("would_change"):
        raise AssertionError(f"upsert_memory dry_run: unexpected answer {out!r}")
    if call("get_memory", slug="smoke-dry-new") is not None:
        raise AssertionError("upsert_memory dry_run: the memory was written")


def _w_set_machine(call: Call) -> None:
    
    
    got = call("set_machine", display_name="smoke")
    if isinstance(got, dict) and got.get("error") not in (None, "machine not found"):
        raise AssertionError(f"set_machine: {got['error']}")
    if not (isinstance(none := call("set_machine"), dict) and "error" in none):
        raise AssertionError(f"set_machine with no field was not refused: {none!r}")


_NO_MACHINE_MATCH = "no known machine matches hostname/home_dir"


def _w_relay_report(call: Call) -> None:
    
    
    got = call("relay_report", kind="token_usage",
              uuid_hint="99999999-0000-4000-8000-000000000099",
              month="2026-01", body=json.dumps({"hostname": "smoke-host", "home_dir": "/nowhere",
                                                "usage": {"a": 1}}))
    if isinstance(got, dict) and got.get("error") not in (None, _NO_MACHINE_MATCH):
        raise AssertionError(f"relay_report token_usage: {got['error']}")
    got = call("relay_report", kind="deps", uuid_hint="99999999-0000-4000-8000-000000000099",
              body=json.dumps({"hostname": "smoke-host", "home_dir": "/nowhere", "at": 1,
                               "fleet": {"ok": True, "missing": [], "broken": [],
                                        "below_floor": []}}))
    if isinstance(got, dict) and got.get("error") not in (None, _NO_MACHINE_MATCH):
        raise AssertionError(f"relay_report deps: {got['error']}")


def _w_upsert_project(call: Call) -> None:
    'Create one project, then rename another with `rename_to` and check its memory moved.'
    _step(call, "upsert_project", canonical_remote="git@github.com:smoke/p.git",
          display_name="SmokeProject")
    _step(call, "upsert_project", canonical_remote="git@github.com:smoke/r.git",
          display_name="SmokeRenameFrom")
    _step(call, "upsert_memory", slug="smoke-rename", memory_type="reference",
          description="about smoke-rename", body="b", load_behavior="lazy",
          project="SmokeRenameFrom")
    _step(call, "upsert_project", canonical_remote="git@github.com:smoke/r.git",
          display_name="SmokeRenameFrom", rename_to="SmokeRenameTo")
    got = call("get_memory", slug="smoke-rename", project="SmokeRenameTo")
    if not isinstance(got, dict) or got.get("scope") != "project:SmokeRenameTo":
        raise AssertionError(f"upsert_project rename_to: the memory did not move: {got!r}")


def _w_run_store_task(call: Call) -> None:
    
    
    got = call("run_store_task", task="smoke-no-such-task")
    if not (isinstance(got, dict) and "unknown task" in str(got.get("error"))):
        raise AssertionError(f"run_store_task: unexpected answer {got!r}")


def _w_send_message(call: Call) -> None:
    'Addressed to a name no session has, so the battery never reaches a real session: the\n    refusal that names no such session is the working answer.'
    args = {"to": "smoke-no-such-session", "message": "smoke"}
    try:
        result = call("send_message", **args)
    except TypeError as exc:
        if "ctx" not in str(exc):
            raise
        result = call("send_message", ctx=None, **args)
    err = _failure(result)
    if err is not None:
        raise AssertionError(f"send_message: {err}")


WRITE_TOOLS: dict[str, Callable[[Call], Any]] = {
    "send_message": _w_send_message,
    "add_audit_observation": lambda c: _observation(c) and None,
    "bulk_edit": _w_bulk_edit,
    "delete_entity": _w_delete_entity,
    "edit_body": _w_edit_body,
    "register_path": _w_register_path,
    "relay_report": _w_relay_report,
    "resolve_audit_observation": _w_resolve_observation,
    "run_store_task": _w_run_store_task,
    "set_entity_links": _w_set_entity_links,
    "set_machine": _w_set_machine,
    "update_audit_observation": _w_update_observation,
    "upsert_agent_definition": lambda c: _step(c, "upsert_agent_definition", name="smoke-agent",
                                               description="d", body="b") and None,
    "upsert_command": lambda c: _step(c, "upsert_command", name="smoke-command", body="b",
                                      description="d") and None,
    "upsert_doc": _w_upsert_doc,
    "upsert_hook": lambda c: _step(c, "upsert_hook", name="smoke-hook", event_type="PreToolUse",
                                   script_body="exit 0", language="sh") and None,
    "upsert_instruction": lambda c: _step(c, "upsert_instruction", title="Smoke instruction",
                                          body="b", load_behavior="lazy") and None,
    "upsert_memory": _w_upsert_memory,
    "upsert_project": _w_upsert_project,
    "upsert_script": lambda c: _step(c, "upsert_script", name="smoke-script",
                                     script_body="exit 0", language="sh") and None,
    "upsert_skill": lambda c: _step(c, "upsert_skill", name="smoke-skill", description="d",
                                    body="b") and None,
}


def run_reads(call: Call) -> dict[str, str]:
    '`{tool: "ok" | error text}` for every read tool, called through `call(tool, **args)`.'
    out: dict[str, str] = {}
    for tool, args in READ_TOOLS.items():
        try:
            try:
                result = call(tool, **args)
            except TypeError as exc:
                if tool not in _NEEDS_CONTEXT or "ctx" not in str(exc):
                    raise
                result = call(tool, ctx=None, **args)
            err = _failure(result)
            out[tool] = "ok" if err is None else err
        except Exception as exc:  
            out[tool] = f"{type(exc).__name__}: {exc}"
    return out


def run_writes(call: Call) -> dict[str, str]:
    '`{tool: "ok" | error text}` for every write tool, called through `call(tool, **args)`.'
    out: dict[str, str] = {}
    for tool, sequence in WRITE_TOOLS.items():
        try:
            sequence(call)
            out[tool] = "ok"
        except Exception as exc:
            out[tool] = f"{type(exc).__name__}: {exc}"
    return out


@contextmanager
def _throwaway_store() -> Iterator[Call]:
    "A `call(tool, **kw)` that runs the server's own tool functions against a fresh empty\n    store in a temp directory. The server's store is put back afterwards."
    from . import server
    from .store import ContextStore
    root = tempfile.mkdtemp(prefix="agent-context-smoke-")
    saved = server._store
    saved_usage = os.environ.get("AGENT_CONTEXT_USAGE")
    os.environ["AGENT_CONTEXT_USAGE"] = "0"       
    try:
        os.makedirs(os.path.join(root, "global"))
        server._store = ContextStore(root=root)

        def call(tool: str, **kw: Any) -> Any:
            return invoke(getattr(server, tool), **kw)
        yield call
    finally:
        server._store = saved
        if saved_usage is None:
            os.environ.pop("AGENT_CONTEXT_USAGE", None)
        else:
            os.environ["AGENT_CONTEXT_USAGE"] = saved_usage
        shutil.rmtree(root, ignore_errors=True)


def _network_call(url: str, token: str) -> Call:
    'A `call(tool, **kw)` that reaches the daemon over the network, one session per call.'
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    def call(tool: str, **kw: Any) -> Any:
        async def one() -> Any:
            headers = {"Authorization": f"Bearer {token}"} if token else None
            async with (create_mcp_http_client(headers=headers) as http,
                        streamable_http_client(url, http_client=http) as (read, write, _),
                        ClientSession(read, write) as session):
                await session.initialize()
                res = await session.call_tool(tool, kw)
                text = getattr(res.content[0], "text", "null") if res.content else "null"
                if res.isError:
                    raise RuntimeError(text)
                return decode(text)
        return asyncio.run(one())
    return call


def _report(kind: str, results: dict[str, str]) -> int:
    failed = 0
    for tool, verdict in results.items():
        if verdict == "ok":
            print(f"PASS {kind:5} {tool}")
        else:
            failed += 1
            print(f"FAIL {kind:5} {tool}: {verdict}")
    return failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_context.smoke", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--url", help="the daemon's MCP endpoint; read tools run against it")
    parser.add_argument("--local", action="store_true",
                        help="run the read tools in this process too, against the throwaway store")
    args = parser.parse_args(argv)
    for noisy in ("httpx", "mcp", "agent-context"):      
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not args.url and not args.local:
        parser.error("give --url (read tools against the live daemon) or --local")

    failed = total = 0
    with _throwaway_store() as local:
        batteries: list[tuple[str, dict[str, str]]] = []
        if args.url:
            net = _network_call(args.url, os.environ.get("AGENT_CONTEXT_TOKEN", ""))
            try:
                net("list_machines")
            except Exception as exc:
                print(f"FAIL cannot reach {args.url}: {type(exc).__name__}: {exc}")
                return 1
            batteries.append(("net", run_reads(net)))
        batteries.append(("write", run_writes(local)))
        if args.local:
            batteries.append(("read", run_reads(local)))   
        for kind, results in batteries:
            failed += _report(kind, results)
            total += len(results)
    print(f"{total - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
