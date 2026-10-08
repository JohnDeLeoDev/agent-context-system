#!/usr/bin/env python3
'A write that failed is not a write: the snapshot must not list it.\n\nThe capture reads tool_use blocks from the transcript, and a call the harness refused or\ntimed out still has one. The matching tool_result carries `is_error: true`. A snapshot\nthat lists the failed call tells the next context that a change landed when it did not.'

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import unittest

CAPTURE = Path(__file__).resolve().parent.parent / "hooks" / "precompact-capture.py"
P = "mcp__agent-context__"


def call(uid: str, tool: str, /, **tool_input: Any) -> dict[str, Any]:
    return {"type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": uid, "name": tool,
                                     "input": tool_input}]}}


def result(uid: str, error: bool) -> dict[str, Any]:
    return {"type": "user",
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": uid,
                                     "is_error": error, "content": "x"}]}}


class FailedWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        scratch = Path.home() / ".cache" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="precompact-failed-", dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "loose").mkdir()

    def capture(self, records: list[Any]) -> dict[str, Any]:
        path = self.root / "t.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        env = dict(os.environ, HOME=str(self.root),
                   AGENT_CONTEXT_STATE_DIR=str(self.root / "state"),
                   AGENT_CONTEXT_STORE=str(self.root / "loose"))
        payload = {"trigger": "auto", "cwd": str(self.root / "loose"), "session_id": "s1",
                   "transcript_path": str(path)}
        subprocess.run([sys.executable, str(CAPTURE)], input=json.dumps(payload),
                       text=True, capture_output=True, env=env, check=True)
        return json.loads((self.root / "state" / "precompact" / "s1.json").read_text())

    def test_an_errored_store_write_is_not_listed(self) -> None:
        rec = self.capture([
            call("a", P + "edit_body", kind="script", key="bad", old_string="x", new_string="y"),
            result("a", True),
            call("b", P + "upsert_hook", name="good"),
            result("b", False),
        ])
        self.assertEqual(rec["store_entities_written"], ["hook:good"])

    def test_an_errored_file_edit_is_not_listed(self) -> None:
        rec = self.capture([
            call("a", "Edit", file_path="/w/bad.py"), result("a", True),
            call("b", "Write", file_path="/w/good.py"), result("b", False),
        ])
        self.assertEqual(rec["edited_this_session"], ["/w/good.py"])

    def test_a_call_with_no_result_is_still_listed(self) -> None:
        rec = self.capture([call("a", P + "upsert_doc", name="pending")])
        self.assertEqual(rec["store_entities_written"], ["doc:pending"])

    def test_a_later_failure_keeps_an_earlier_success(self) -> None:
        rec = self.capture([
            call("a", P + "upsert_hook", name="h"), result("a", False),
            call("b", P + "upsert_hook", name="h"), result("b", True),
        ])
        self.assertEqual(rec["store_entities_written"], ["hook:h"])

    def test_a_result_for_an_unknown_id_is_ignored(self) -> None:
        rec = self.capture([call("a", P + "upsert_hook", name="h"), result("zzz", True)])
        self.assertEqual(rec["store_entities_written"], ["hook:h"])

    def test_result_records_are_still_not_user_asks(self) -> None:
        rec = self.capture([result("a", True)])
        self.assertEqual(rec["recent_user_asks"], [])


if __name__ == "__main__":
    unittest.main()
