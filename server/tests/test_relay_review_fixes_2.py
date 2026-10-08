'Second review round for the relay self-refresh work (C1 to C3).\n\nBehavior the implementation provides, all in `agent_context.relay_update`:\n  C1  one predicate, shared by `_watch_respawn` and `note_relay_start`: a relay record counts as a\n      respawn only when its pid differs from the exited relay\'s pid AND its timestamp is not\n      earlier than the `ts` in ~/.cache/agent-context/relay-exit.json (a missing or corrupt\n      exit record gives a floor of 0.0). `note_relay_start` uses the current time as its\n      timestamp and flips a health record with `respawned: false` to true only by that rule.\n  C2  `start_etag` and `_installed_etag` return "" unless the file text, stripped, is 1 to 200\n      printable ASCII characters (space to tilde). Anything else (non-ASCII, control characters,\n      empty, over-length, bytes that are not UTF-8) is "no etag": no header, no If-None-Match,\n      and nothing raises.\n  C3  `arm()` calls `start_etag(home)` before anything that touches the network, so `schedule()`\n      cannot change the captured value.'
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from agent_context import identity
from agent_context import relay_update as U
from relay_self_refresh_support import *  
from relay_self_refresh_support import (CURRENT, STALE, LsState, Rig, alive_record,
                                        conditional_gets, etag_file, exit_record,
                                        health_record, need, read_json, store_etag)
from test_relay_review_fixes import _sent_header, remote_relay  

ETAG_HEADER = "x-relay-source-etag"
EXITED_PID = 987_654




SCENARIOS = [
    
    ("earlier_unrelated_relay", 1000.0, False, False),   
    ("later_relay", -1000.0, False, True),
    ("same_pid", -1000.0, True, False),
]




ETAG_FILES = [
    ("plain", b'"v1"\n', '"v1"'),
    ("non_ascii", '"vé"\n'.encode(), ""),
    ("control_character", b'v\x01v\n', ""),
    ("inner_tab", b'v\t1\n', ""),
    ("empty", b"", ""),
    ("only_newline", b"\n", ""),
    ("201_characters", b"a" * 201 + b"\n", ""),
    ("200_characters", b"a" * 200 + b"\n", "a" * 200),
    ("surrounding_whitespace", b'  \t "v1" \r\n', '"v1"'),
    ("whitespace_around_201", b"  " + b"a" * 201 + b"  \n", ""),
    ("not_utf8", b"\xff\xfe\n", ""),
]
ETAG_IDS = [c[0] for c in ETAG_FILES]


def _write_raw_etag(home: Path, raw: bytes) -> None:
    etag_file(home).parent.mkdir(parents=True, exist_ok=True)
    etag_file(home).write_bytes(raw)


def _call(fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception as exc:  
        assert False, f"{getattr(fn, '__name__', fn)} raised {exc!r}"


@pytest.mark.parametrize("name,raw,expected", ETAG_FILES, ids=ETAG_IDS)
def test_start_etag_keeps_only_a_valid_etag(name: str, raw: bytes, expected: str,
                                            home: Path) -> None:
    _write_raw_etag(home, raw)
    assert _call(need(U, "start_etag"), home) == expected, name


@pytest.mark.parametrize("name,raw,expected", ETAG_FILES, ids=ETAG_IDS)
def test_the_installed_etag_read_keeps_only_a_valid_etag(name: str, raw: bytes, expected: str,
                                                         home: Path) -> None:
    _write_raw_etag(home, raw)
    assert _call(need(U, "_installed_etag"), home) == expected, name


@pytest.mark.parametrize("name,raw,expected", ETAG_FILES, ids=ETAG_IDS)
def test_the_reporting_header_matches_the_validated_start_etag(
        name: str, raw: bytes, expected: str, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, remote_relay: None) -> None:
    relay_home = tmp_path / "relay-home"
    relay_home.mkdir()
    monkeypatch.setenv("HOME", str(relay_home))
    _write_raw_etag(relay_home, raw)
    headers = _call(identity.local_headers)
    assert _sent_header(headers) == (expected or None), (name, headers)



