#!/usr/bin/env python3
'Acceptance tests for transcript_records.py (context overhaul, Phase 1 item 6).\n\nThe Stop chain read the same transcript about six times per turn end: 27 ms a parse\non a 4.3 MB transcript, growing with the session. hook-dispatch.py runs every Python\nguard in one process, so one cached parse serves them all.\n\n    python3 test-transcript-records.py'
import importlib.util
import json
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.join(HERE, "..", "hooks")
sys.path.insert(0, HERE)
import transcript_records as tr  


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load %s" % path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def prompt(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def tool_use(name, **inp):
    return {"type": "tool_use", "name": name, "input": inp}


def text(t):
    return {"type": "text", "text": t}


def write(root, lines):
    path = os.path.join(root, "t.jsonl")
    with open(path, "wb") as f:
        for ln in lines:
            f.write((ln if isinstance(ln, bytes) else json.dumps(ln).encode()) + b"\n")
    return path



def test_records_in_order_and_tolerant(root):
    path = write(root, [prompt("go"), b"{not json", b"[1, 2]",
                        b'{"type": "assistant", "note": "caf\xe9"}',
                        assistant(text("done"))])
    got = tr.records(path)
    assert [r.get("type") for r in got] == ["user", "assistant", "assistant"], got
    assert all(isinstance(r, dict) for r in got)


def test_missing_or_empty_path_is_empty(root):
    assert tuple(tr.records(os.path.join(root, "nope.jsonl"))) == ()
    assert tuple(tr.records("")) == ()
    assert tuple(tr.records(None)) == ()



def test_cache_by_path_size_and_mtime(root):
    path = write(root, [prompt("go"), assistant(text("a"))])
    before = tr.PARSES
    first = tr.records(path)
    assert tr.PARSES == before + 1
    again = tr.records(path)
    assert tr.PARSES == before + 1 and list(again) == list(first)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(prompt("more")) + "\n")
    third = tr.records(path)
    assert tr.PARSES == before + 2 and len(third) == 3, third



def test_every_stop_reader_shares_one_parse(root):
    vcj = load(os.path.join(HERE, "verification-claim-judge.py"), "vcj_t")
    rsq = load(os.path.join(HOOKS, "require-structured-questions.py"), "rsq_t")
    bs = load(os.path.join(HOOKS, "block-stop-with-open-work.py"), "bs_t")
    lt = load(os.path.join(HOOKS, "locked-test-drift-gate.py"), "lt_t")
    path = write(root, [
        prompt("first"), assistant(text("ok")),
        prompt("fix it"),
        assistant(tool_use("Edit", file_path="/w/app.py", old_string="a", new_string="b")),
        assistant(tool_use("AskUserQuestion", questions=[])),
        assistant(text("All tests pass.")),
    ])
    before = tr.PARSES
    turn = vcj.previous_turn(path)
    assert [r["type"] for r in turn] == ["assistant"] * 3, turn
    assert vcj.judge_transcript(path) == "All tests pass."
    assert vcj.judge_unverified_edit(path) == ["/w/app.py"]
    assert rsq.asked_structurally(path) is True
    asked, wakes, said = bs.read_turn(path)
    assert (asked, wakes, said) == (True, False, "fix it")
    tools = lt.tool_records(path)
    assert len(tools) == 2 and all('"tool_use"' in json.dumps(r) for r in tools), tools
    assert tr.PARSES == before + 1, tr.PARSES - before


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        with tempfile.TemporaryDirectory() as root:
            try:
                fn(root)
                print(f"ok    {name}")
            except Exception:
                failed += 1
                print(f"FAIL  {name}")
                traceback.print_exc(limit=1)
    print(f"{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
