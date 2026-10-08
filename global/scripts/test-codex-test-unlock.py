'Codex structured-reply approval for locked acceptance tests.'

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks/codex-test-unlock.py"
SYNC = ROOT / "scripts/home-settings-sync.py"
GUARD = ROOT / "hooks/block-consent-self-grant.py"


class CodexTestUnlockTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = Path.home() / ".cache/tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.target = base / "locked_test.py"
        self.target.write_text("assert True\n")
        self.scripts = base / "scripts"
        self.scripts.mkdir()
        self.calls = base / "consent-calls"
        (self.scripts / "test-lock.py").write_text(
            "import os\n"
            "def locked_entry(path):\n"
            "    return (object(), path) if path == os.environ['FAKE_LOCK_PATH'] else None\n"
            "def is_locked(path):\n"
            "    return locked_entry(path) is not None\n"
        )
        (self.scripts / "test-lock-consent.py").write_text(
            "import os, sys\n"
            "with open(os.environ['FAKE_CONSENT_LOG'], 'a') as out:\n"
            "    out.write(sys.argv[1] + '\\n')\n"
            "print('Unlocked: ' + sys.argv[1])\n"
        )
        self.env = dict(os.environ)
        self.env.update(
            APPROVAL_SCRIPTS_DIR=str(self.scripts),
            TEST_LOCK_TOOL=str(self.scripts / "test-lock.py"),
            XDG_STATE_HOME=str(base / "state"),
            FAKE_LOCK_PATH=str(self.target),
            FAKE_CONSENT_LOG=str(self.calls),
        )

    def reply(self, answer: str, *, question: str | None = None, call: str = "call_123") -> str:
        title = question or (
            f"Unlock the locked test {self.target}? "
            f"[approval:test-unlock:{self.target}:0]"
        )
        item = {
            "questionItemId": json.dumps(["request_user_input_async", call, 0]),
            "question": title,
            "answer": answer,
        }
        return "<send_user_message_question_reply>\n" + json.dumps([item]) + "\n</send_user_message_question_reply>"

    def run_hook(self, prompt: str, event: str = "UserPromptSubmit") -> subprocess.CompletedProcess[str]:
        self.assertTrue(HOOK.exists(), "Codex test-unlock hook is missing")
        payload = {"hook_event_name": event, "turn_id": "turn_1", "session_id": "session_1", "prompt": prompt}
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=self.env,
        )

    def call_count(self) -> int:
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def test_exact_approval_unlocks_once_and_logs(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("approval_question", ROOT / "hooks/approval-question.py")
        assert spec and spec.loader
        approval = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(approval)
        approval.RECORDS = str(Path(self.env["XDG_STATE_HOME"]) / "agent-context" / "approval-marks")
        title = f"Unlock the locked test {self.target}? [approval:test-unlock:{self.target}:0]"
        approval.leave_record({"session_id": "session_1", "tool_use_id": "call_123"},
                              [{"question": title}])
        result = self.run_hook(self.reply("Approve"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.call_count(), 1, result.stdout + result.stderr)
        self.assertIn("granted", result.stdout)
        self.assertIn(str(self.target), result.stdout)
        self.assertEqual(self.run_hook(self.reply("Approve")).returncode, 0)
        self.assertEqual(self.call_count(), 1, "same reply ID must not grant twice")

    def test_denial_and_malformed_reply_do_not_grant(self) -> None:
        self.assertEqual(self.run_hook(self.reply("Approve", call="call_forged")).returncode, 0)
        self.assertEqual(self.call_count(), 0)
        prompts = [
            self.reply("Deny", call="call_deny"),
            self.reply("Approve (Recommended)", call="call_free_text"),
            self.reply("Approve (Recommended)", question="Unlock anything?", call="call_bad_shape"),
            "Approve the unlock please",
            "<send_user_message_question_reply>not JSON</send_user_message_question_reply>",
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertEqual(self.run_hook(prompt).returncode, 0)
        self.assertEqual(self.call_count(), 0)

    def test_mismatched_and_stale_pending_marks_do_not_grant(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("approval_question", ROOT / "hooks/approval-question.py")
        assert spec and spec.loader
        approval = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(approval)
        approval.RECORDS = str(Path(self.env["XDG_STATE_HOME"]) / "agent-context" / "approval-marks")
        title = f"Unlock the locked test {self.target}? [approval:test-unlock:{self.target}:0]"

        approval.leave_record({"session_id": "session_1", "tool_use_id": "call_mismatch"},
                              [{"question": "another question"}])
        self.assertEqual(self.run_hook(self.reply("Approve", call="call_mismatch")).returncode, 0)
        self.assertEqual(self.call_count(), 0)

        approval.leave_record({"session_id": "session_1", "tool_use_id": "call_stale"},
                              [{"question": title}])
        mark = Path(approval.RECORDS) / "id-call_stale"
        os.utime(mark, (0, 0))
        self.assertEqual(self.run_hook(self.reply("Approve", call="call_stale")).returncode, 0)
        self.assertEqual(self.call_count(), 0)

    def test_other_event_and_other_target_do_not_grant(self) -> None:
        other = self.target.with_name("other.py")
        self.assertEqual(self.run_hook(self.reply("Approve (Recommended)"), "Stop").returncode, 0)
        self.assertEqual(
            self.run_hook(self.reply("Approve (Recommended)", question=(
                f"Unlock the locked test {other}? [approval:test-unlock:{other}:0]"
            ), call="call_other")).returncode,
            0,
        )
        self.assertEqual(self.call_count(), 0)

    def test_wiring_and_self_grant_guard(self) -> None:
        self.assertIn('"codex-test-unlock.py"', SYNC.read_text())
        self.assertIn('"codex-test-unlock"', GUARD.read_text())


if __name__ == "__main__":
    unittest.main()
