#!/usr/bin/env python3
'Store entities written through MCP tools belong in the compaction snapshot.\n\nEdit and Write never touch a store entity: the store blocks them, so a session edits a\nhook, script or doc through edit_body or an upsert_* tool. Those calls carry an entity\nname, not a file path, so the capture records them as `kind:name` under\n`store_entities_written`, and the delivery hook renders them.'

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import unittest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
CAPTURE = HOOKS / "precompact-capture.py"
DELIVER = HOOKS / "compact-invalidates-bootstrap.py"
P = "mcp__agent-context__"


def tool_use(tool: str, /, **tool_input: Any) -> dict[str, Any]:
    return {"type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": "t1", "name": tool,
                                     "input": tool_input}]}}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        scratch = Path.home() / ".cache" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="precompact-store-", dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.loose = self.root / "loose"
        self.loose.mkdir()

    def env(self) -> dict[str, str]:
        base = dict(os.environ)
        base.update({"HOME": str(self.root), "AGENT_CONTEXT_STATE_DIR": str(self.state),
                     "AGENT_CONTEXT_STORE": str(self.loose)})
        base.pop("AGENT_CONTEXT_PRECOMPACT_CONTEXT", None)
        base.pop("AGENT_CONTEXT_PRECOMPACT_MAX_CHARS", None)
        return base

    def capture(self, records: list[Any]) -> dict[str, Any]:
        path = self.root / "t.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        payload = {"trigger": "auto", "cwd": str(self.loose), "session_id": "s1",
                   "transcript_path": str(path)}
        subprocess.run([sys.executable, str(CAPTURE)], input=json.dumps(payload),
                       text=True, capture_output=True, env=self.env(), check=True)
        return json.loads((self.state / "precompact" / "s1.json").read_text())


class CaptureTest(Base):
    def test_edit_body_and_upserts_are_recorded_newest_last(self) -> None:
        rec = self.capture([
            tool_use(P + "edit_body", kind="hook", key="a", old_string="x", new_string="y"),
            tool_use(P + "upsert_script", name="b", script_body="#"),
            tool_use(P + "upsert_hook", name="c"),
            tool_use(P + "edit_body", kind="hook", key="a", old_string="y", new_string="z"),
        ])
        self.assertEqual(rec["store_entities_written"],
                         ["script:b", "hook:c", "hook:a"])

    def test_reads_and_other_servers_are_not_writes(self) -> None:
        rec = self.capture([
            tool_use(P + "get_entity", kind="hook", key="read-only"),
            tool_use(P + "search_all", query="q"),
            tool_use("mcp__other__upsert_script", name="foreign"),
            tool_use(P + "upsert_doc", name="d"),
        ])
        self.assertEqual(rec["store_entities_written"], ["doc:d"])

    def test_a_write_with_no_name_is_skipped(self) -> None:
        rec = self.capture([
            tool_use(P + "upsert_hook"),
            tool_use(P + "edit_body", kind="hook"),
            tool_use(P + "upsert_hook", name=""),
            tool_use(P + "upsert_hook", name=["not", "a", "string"]),
            tool_use(P + "upsert_hook", name="ok"),
        ])
        self.assertEqual(rec["store_entities_written"], ["hook:ok"])

    def test_capped_at_thirty_keeping_the_newest(self) -> None:
        rec = self.capture([tool_use(P + "upsert_script", name="s%d" % i)
                            for i in range(40)])
        got = rec["store_entities_written"]
        self.assertEqual(len(got), 30)
        self.assertEqual(got[0], "script:s10")
        self.assertEqual(got[-1], "script:s39")

    def test_file_edits_are_unchanged(self) -> None:
        rec = self.capture([tool_use("Edit", file_path="/w/a.py"),
                            tool_use(P + "upsert_hook", name="h")])
        self.assertEqual(rec["edited_this_session"], ["/w/a.py"])


class DeliveryTest(Base):
    def deliver(self, record: dict[str, Any]) -> str:
        out = self.state / "precompact"
        out.mkdir(parents=True)
        (out / "s1.json").write_text(json.dumps(record))
        payload = {"session_id": "s1", "transcript_path": str(self.root / "none.jsonl")}
        proc = subprocess.run([sys.executable, str(DELIVER)], input=json.dumps(payload),
                              text=True, capture_output=True, env=self.env(), check=True)
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_entities_are_rendered_oldest_first(self) -> None:
        text = self.deliver({"store_entities_written": ["hook:a", "script:b"]})
        self.assertIn("Store entities written (2, oldest first):", text)
        self.assertLess(text.index("hook:a"), text.index("script:b"))

    def test_a_record_without_the_key_renders_as_before(self) -> None:
        text = self.deliver({"edited_this_session": ["/w/a.py"]})
        self.assertNotIn("Store entities written", text)
        self.assertIn("/w/a.py", text)


if __name__ == "__main__":
    unittest.main()
