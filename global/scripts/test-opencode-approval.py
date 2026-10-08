"opencode question-tool approval: opencode-approval.py grants only user's Approve."

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks/opencode-approval.py"


class OpencodeApprovalTests(unittest.TestCase):
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
            "def is_locked(path):\n"
            "    return path == os.environ['FAKE_LOCK_PATH']\n"
        )
        (self.scripts / "test-lock-consent.py").write_text(
            "import os, sys\n"
            "with open(os.environ['FAKE_CONSENT_LOG'], 'a') as out:\n"
            "    out.write(sys.argv[1] + '\\n')\n"
            "print('Unlocked: ' + sys.argv[1])\n"
        )
        self.state = base / "state"
        self.env = dict(os.environ)
        self.env.update(
            APPROVAL_SCRIPTS_DIR=str(self.scripts),
            TEST_LOCK_TOOL=str(self.scripts / "test-lock.py"),
            XDG_STATE_HOME=str(self.state),
            FAKE_LOCK_PATH=str(self.target),
            FAKE_CONSENT_LOG=str(self.calls),
        )
        self.title = f"Unlock the locked test {self.target}? [approval:test-unlock:{self.target}:0]"

    def question(self, **changes: object) -> dict[str, object]:
        base: dict[str, object] = {
            "question": self.title, "header": "Approval", "multiple": False,
            "options": [{"label": "Approve", "description": "the assertion is wrong"},
                        {"label": "Deny", "description": "Refuse it"}],
        }
        base.update(changes)
        return base

    def run_hook(self, event: str, questions: list[dict[str, object]], *, call: str = "call_1",
                 answers: object = None, tool: str = "question",
                 extra_input: dict[str, object] | None = None) -> subprocess.CompletedProcess[str]:
        tool_input: dict[str, object] = {"questions": questions}
        tool_input.update(extra_input or {})
        payload: dict[str, object] = {"hook_event_name": event, "tool_name": tool,
                                      "session_id": "ses_1", "tool_use_id": call,
                                      "tool_input": tool_input}
        if answers is not None:
            payload["tool_response"] = {"answers": answers}
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                              text=True, capture_output=True, env=self.env)

    def call_count(self) -> int:
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def marks(self) -> list[str]:
        records = self.state / "agent-context" / "approval-marks"
        return sorted(p.name for p in records.iterdir()) if records.exists() else []

    def test_exact_question_approved_grants_once(self) -> None:
        pre = self.run_hook("PreToolUse", [self.question()])
        self.assertEqual(pre.returncode, 0, pre.stderr)
        self.assertIn("id-call_1", self.marks())
        post = self.run_hook("PostToolUse", [self.question()], answers=[["Approve"]])
        self.assertEqual(post.returncode, 0, post.stderr)
        self.assertIn("granted", post.stdout)
        self.assertIn(str(self.target), post.stdout)
        self.assertEqual(self.call_count(), 1)
        log = (self.state / "agent-context" / "test-lock-consent.log").read_text()
        self.assertIn("session=ses_1", log)
        self.assertIn("tool_use=call_1", log)
        again = self.run_hook("PostToolUse", [self.question()], answers=[["Approve"]])
        self.assertIn("not granted", again.stdout)
        self.assertEqual(self.call_count(), 1, "one reply grants once")

    def test_pre_refuses_inexact_or_bundled_approval_questions(self) -> None:
        other = {"question": "Which colour?", "header": "Colour", "options": [{"label": "Red"}]}
        refused = {
            "bundled": [self.question(), other],
            "two approvals": [self.question(), self.question()],
            "altered text": [self.question(question="Please " + self.title)],
            "wrong header": [self.question(header="Unlock")],
            "multiple": [self.question(multiple=True)],
            "extra option": [self.question(options=[{"label": "Approve"}, {"label": "Deny"},
                                                    {"label": "Later"}])],
            "tag only, other header": [self.question(header="Colour")],
        }
        for name, questions in refused.items():
            with self.subTest(name=name):
                result = self.run_hook("PreToolUse", questions, call="call_" + name.split()[0])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("opencode-approval", result.stderr)
        self.assertEqual(self.marks(), [])
        plain = self.run_hook("PreToolUse", [other])
        self.assertEqual((plain.returncode, plain.stderr), (0, ""))
        self.assertEqual(self.marks(), [])

    def test_other_answers_do_not_grant(self) -> None:
        answers: dict[str, object] = {
            "deny": [["Deny"]],
            "typed": [["Approve it please"]],
            "both": [["Approve", "Deny"]],
            "empty": [[]],
            "unanswered": [],
            "two": [["Approve"], ["Approve"]],
            "string": "Approve",
        }
        for name, value in answers.items():
            with self.subTest(name=name):
                call = "call_" + name
                self.assertEqual(self.run_hook("PreToolUse", [self.question()], call=call).returncode, 0)
                post = self.run_hook("PostToolUse", [self.question()], call=call, answers=value)
                self.assertEqual(post.returncode, 0, post.stderr)
                self.assertNotIn("granted.", post.stdout)
        self.assertEqual(self.call_count(), 0)
        self.assertIn("denied by user", self.run_hook_after_pre("call_y", [["Deny"]]).stdout)

    def run_hook_after_pre(self, call: str, answers: object) -> subprocess.CompletedProcess[str]:
        self.assertEqual(self.run_hook("PreToolUse", [self.question()], call=call).returncode, 0)
        return self.run_hook("PostToolUse", [self.question()], call=call, answers=answers)

    def test_answer_without_a_pre_mark_does_not_grant(self) -> None:
        forged = self.run_hook("PostToolUse", [self.question()], call="call_forged",
                               answers=[["Approve"]])
        self.assertIn("not granted", forged.stdout)
        self.assertEqual(self.run_hook("PreToolUse", [self.question()], call="call_a").returncode, 0)
        other_call = self.run_hook("PostToolUse", [self.question()], call="call_b",
                                   answers=[["Approve"]])
        self.assertIn("not granted", other_call.stdout)
        self.assertEqual(self.call_count(), 0)

    def test_agent_written_answers_and_changed_question_do_not_grant(self) -> None:
        self.assertEqual(self.run_hook("PreToolUse", [self.question()], call="call_in").returncode, 0)
        in_input = self.run_hook("PostToolUse", [self.question()], call="call_in",
                                 extra_input={"answers": [["Approve"]]})
        self.assertIn("not granted", in_input.stdout)
        other = self.target.with_name("other.py")
        self.assertEqual(self.run_hook("PreToolUse", [self.question()], call="call_sw").returncode, 0)
        swapped = self.run_hook("PostToolUse", [self.question(question=(
            f"Unlock the locked test {other}? [approval:test-unlock:{other}:0]"))],
            call="call_sw", answers=[["Approve"]])
        self.assertIn("not granted", swapped.stdout)
        self.assertEqual(self.call_count(), 0)

    def test_unsafe_call_id_and_other_tools(self) -> None:
        odd = "call/with spaces:1"
        self.assertEqual(self.run_hook("PreToolUse", [self.question()], call=odd).returncode, 0)
        self.assertTrue(any(name.startswith("id-h") for name in self.marks()), self.marks())
        post = self.run_hook("PostToolUse", [self.question()], call=odd, answers=[["Approve"]])
        self.assertIn("granted", post.stdout)
        self.assertEqual(self.call_count(), 1)
        ignored = self.run_hook("PostToolUse", [self.question()], call="call_t",
                                answers=[["Approve"]], tool="AskUserQuestion")
        self.assertEqual((ignored.returncode, ignored.stdout), (0, ""))
        self.assertEqual(self.call_count(), 1)

    def test_stale_mark_does_not_grant(self) -> None:
        self.assertEqual(self.run_hook("PreToolUse", [self.question()], call="call_old").returncode, 0)
        mark = self.state / "agent-context" / "approval-marks" / "id-call_old"
        os.utime(mark, (0, 0))
        post = self.run_hook("PostToolUse", [self.question()], call="call_old", answers=[["Approve"]])
        self.assertIn("not granted", post.stdout)
        self.assertEqual(self.call_count(), 0)

    def test_plugin_wiring_and_self_grant_guard(self) -> None:
        generator = (ROOT / "scripts/harness-materialize.py").read_text()
        for name in ("opencode-approval.py", "block-locked-test-edit.py",
                     "locked-test-drift-gate.py", "block-consent-self-grant.py"):
            self.assertIn('"%s"' % name, generator)
        self.assertIn('ctx.tool.hook("execute.after"', generator)
        guard = ROOT / "hooks/block-consent-self-grant.py"
        self.assertIn('"opencode-approval": LOCK_DENY', guard.read_text())
        commands = {
            "curl -X POST http://127.0.0.1:4096/api/session/ses_1/form/frm_1/reply -d '{}'": 2,
            "R=reply; curl -X POST \"http://h/api/session/s/form/f/$R\"": 2,
            "curl -X POST http://h/question/q1/re\"\"ply": 2,
            "python3 ~/.agent-context/global/hooks/opencode-approval.py < payload.json": 2,
            "curl http://127.0.0.1:4096/api/session/ses_1/form": 0,
            "grep -n grant ~/.agent-context/global/hooks/opencode-approval.py": 0,
        }
        for command, expected in commands.items():
            with self.subTest(command=command):
                payload = {"tool_name": "Bash", "tool_input": {"command": command}}
                result = subprocess.run([sys.executable, str(guard)], input=json.dumps(payload),
                                        text=True, capture_output=True, env=self.env)
                self.assertEqual(result.returncode, expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
