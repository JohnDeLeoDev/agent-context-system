#!/usr/bin/env python3
'Regression tests for precompact-capture\'s user-ask filter.\n\nFound by reviewing the hook against a real transcript: 7 of the last 10 recorded asks\nwere <task-notification> messages and one was the compaction summary. Real records mark\nthem with `origin.kind == "task-notification"` and `isCompactSummary`, and their text\nstarts with a fixed prefix, so all three signals are checked.'

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import unittest

CAPTURE = Path(__file__).resolve().parent.parent / "hooks" / "precompact-capture.py"


def user(content: Any, **extra: Any) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": content}, **extra}


class NoiseTest(unittest.TestCase):
    def setUp(self) -> None:
        scratch = Path.home() / ".cache" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="precompact-noise-", dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def asks(self, records: list[dict[str, Any]]) -> list[str]:
        transcript = self.root / "t.jsonl"
        transcript.write_text("".join(json.dumps(r) + "\n" for r in records))
        state = self.root / "state"
        env = dict(os.environ, AGENT_CONTEXT_STATE_DIR=str(state),
                   AGENT_CONTEXT_STORE=str(self.root))
        payload = {"session_id": "s1", "trigger": "auto", "cwd": str(self.root),
                   "transcript_path": str(transcript)}
        subprocess.run([sys.executable, str(CAPTURE)], input=json.dumps(payload),
                       text=True, capture_output=True, env=env, check=True)
        rec = json.loads((state / "precompact" / "s1.json").read_text())
        return rec.get("recent_user_asks", [])

    def test_task_notification_by_origin_is_not_an_ask(self) -> None:
        got = self.asks([user("real ask"),
                         user("finished job output", origin={"kind": "task-notification"})])
        self.assertEqual(got, ["real ask"])

    def test_task_notification_by_prefix_is_not_an_ask(self) -> None:
        got = self.asks([user("real ask"),
                         user("<task-notification>\n<task-id>b1</task-id>\n</task-notification>")])
        self.assertEqual(got, ["real ask"])

    def test_compaction_summary_by_flag_is_not_an_ask(self) -> None:
        got = self.asks([user("real ask"),
                         user("a long summary body", isCompactSummary=True,
                              isVisibleInTranscriptOnly=True)])
        self.assertEqual(got, ["real ask"])

    def test_compaction_summary_by_prefix_is_not_an_ask(self) -> None:
        got = self.asks([user("real ask"),
                         user("This session is being continued from a previous conversation "
                              "that ran out of context. The summary below covers it.")])
        self.assertEqual(got, ["real ask"])

    def test_a_real_ask_that_mentions_the_words_is_kept(self) -> None:
        got = self.asks([user("why did the task-notification arrive twice?"),
                         user("This session is being slow, please look")])
        self.assertEqual(len(got), 2)


if __name__ == "__main__":
    unittest.main()
