#!/usr/bin/env python3
"Tests for the transcript-derived half of the compaction snapshot.\n\nprecompact-capture.py reads the transcript and records what git cannot: every file the\nsession edited (a committed file drops out of `git status`) and the user's last few real\nasks. compact-invalidates-bootstrap.py hands that back after the compaction, framed as a\nhistorical record, bounded, and switchable off."

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

CAP_ENV = "AGENT_CONTEXT_PRECOMPACT_MAX_CHARS"
OFF_ENV = "AGENT_CONTEXT_PRECOMPACT_CONTEXT"


def user(content: Any, **extra: Any) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": content}, **extra}


def tool_use(name: str, **tool_input: Any) -> dict[str, Any]:
    return {"type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": "t1", "name": name,
                                     "input": tool_input}]}}


def tool_result() -> dict[str, Any]:
    return user([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}])


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, check=True)


def signed_repo(repo: Path) -> None:
    'A repo that signs with a throwaway key: no commit in a fixture is ever unsigned.'
    key = repo.parent / "signing-key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                   check=True, capture_output=True)
    os.chmod(key, 0o600)             
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "gpg.format", "ssh")
    git(repo, "config", "gpg.ssh.program", "ssh-keygen")
    git(repo, "config", "user.signingkey", str(key) + ".pub")
    git(repo, "config", "commit.gpgsign", "true")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        scratch = Path.home() / ".cache" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="precompact-transcript-", dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.loose = self.root / "loose"
        self.loose.mkdir()

    def env(self, **extra: str) -> dict[str, str]:
        base = {k: v for k, v in os.environ.items() if k not in (CAP_ENV, OFF_ENV)}
        base.update({"HOME": str(self.root), "AGENT_CONTEXT_STATE_DIR": str(self.state),
                     "AGENT_CONTEXT_STORE": str(self.loose)})
        base.update(extra)
        return base


class CaptureTest(Base):
    def capture(self, records: list[Any] | None = None, *, raw: str | None = None,
                cwd: Path | None = None, transcript: str | None = "t.jsonl",
                session: str | None = "s1") -> tuple[subprocess.CompletedProcess[str],
                                                      dict[str, Any] | None]:
        path = self.root / "t.jsonl"
        if raw is not None:
            path.write_text(raw)
        elif records is not None:
            path.write_text("".join(json.dumps(r) + "\n" for r in records))
        payload: dict[str, Any] = {"trigger": "auto", "cwd": str(cwd or self.loose)}
        if session is not None:
            payload["session_id"] = session
        if transcript is not None:
            payload["transcript_path"] = str(self.root / transcript)
        proc = subprocess.run([sys.executable, str(CAPTURE)], input=json.dumps(payload),
                              text=True, capture_output=True, env=self.env())
        out = self.state / "precompact" / "s1.json"
        return proc, (json.loads(out.read_text()) if out.exists() else None)

    
    def test_edited_files_listed_once_most_recent_last(self) -> None:
        _, rec = self.capture([
            tool_use("Edit", file_path="/w/a.py"),
            tool_use("Write", file_path="/w/b.py"),
            tool_use("MultiEdit", file_path="/w/c.py"),
            tool_use("NotebookEdit", notebook_path="/w/n.ipynb"),
            tool_use("Read", file_path="/w/read-only.py"),
            tool_use("Bash", command="touch /w/shell.py"),
            tool_use("Edit", file_path="/w/a.py"),
        ])
        assert rec is not None
        self.assertEqual(rec["edited_this_session"],
                         ["/w/b.py", "/w/c.py", "/w/n.ipynb", "/w/a.py"])

    
    def test_committed_edit_survives_a_clean_status(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        signed_repo(repo)
        (repo / "a.py").write_text("one\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "first")
        _, rec = self.capture([tool_use("Edit", file_path=str(repo / "a.py"))], cwd=repo)
        assert rec is not None
        self.assertEqual(rec["work"]["modified"], [])
        self.assertEqual(rec["edited_this_session"], [str(repo / "a.py")])

    def test_edited_files_capped_at_thirty_keeping_the_newest(self) -> None:
        _, rec = self.capture([tool_use("Write", file_path="/w/f%d.py" % i) for i in range(35)])
        assert rec is not None
        files = rec["edited_this_session"]
        self.assertEqual(len(files), 30)
        self.assertEqual(files[0], "/w/f5.py")
        self.assertEqual(files[-1], "/w/f34.py")
        self.assertNotIn("/w/f4.py", files)

    def test_unusable_transcripts_give_empty_lists_and_never_fail(self) -> None:
        cases: list[dict[str, Any]] = [
            {"transcript": None},                       
            {"transcript": "absent.jsonl"},             
            {"raw": ""},                                
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                proc, rec = self.capture(**kwargs)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                assert rec is not None
                self.assertEqual(rec["edited_this_session"], [])
                self.assertEqual(rec["recent_user_asks"], [])

    def test_a_malformed_line_is_skipped_and_the_rest_kept(self) -> None:
        good = json.dumps(tool_use("Edit", file_path="/w/a.py"))
        later = json.dumps(user("still here"))
        proc, rec = self.capture(raw="%s\n{not json\n%s\n" % (good, later))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        assert rec is not None
        self.assertEqual(rec["edited_this_session"], ["/w/a.py"])
        self.assertEqual(rec["recent_user_asks"], ["still here"])

    
    def test_recent_asks_keep_the_last_ten_truncated(self) -> None:
        records = [user("ask %d" % i) for i in range(12)]
        records.append(user("x" * 500))
        _, rec = self.capture(records)
        assert rec is not None
        asks = rec["recent_user_asks"]
        self.assertEqual(len(asks), 10)
        self.assertEqual(asks[0], "ask 3")
        self.assertLessEqual(max(len(a) for a in asks), 200)
        self.assertTrue(asks[-1].startswith("xxxx"))
        self.assertTrue(asks[-1].endswith("…"))

    
    def test_only_real_user_asks_are_kept(self) -> None:
        reminder = "<system-reminder>\nhook context\n</system-reminder>"
        _, rec = self.capture([
            user("first real ask"),
            tool_result(),
            user("meta injected", isMeta=True),
            user("subagent prompt", isSidechain=True),
            user([{"type": "text", "text": reminder + "\nsecond real ask"}]),
            user(reminder),
            user("<command-name>/clear</command-name>"),
            user("<local-command-stdout>ok</local-command-stdout>"),
            user("Stop hook feedback:\n[x] BLOCKED"),
            user("<ci-monitor-event>build failed</ci-monitor-event>"),
            user("[Request interrupted by user]"),
            user("third   real\nask"),
        ])
        assert rec is not None
        self.assertEqual(rec["recent_user_asks"],
                         ["first real ask", "second real ask", "third real ask"])

    
    def test_existing_record_keys_and_silence_are_unchanged(self) -> None:
        proc, rec = self.capture([user("hi")])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        assert rec is not None
        for key in ("session", "trigger", "work", "store"):
            self.assertIn(key, rec)
        self.assertEqual(rec["session"], "s1")
        self.assertEqual(rec["trigger"], "auto")
        self.assertEqual(rec["work"]["cwd"], str(self.loose))

    def test_no_session_id_writes_nothing(self) -> None:
        proc, rec = self.capture([user("hi")], session=None)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIsNone(rec)


class DeliveryTest(Base):
    DIRECTIVE = "mcp__agent-context__get_session_context"

    def deliver(self, record: dict[str, Any] | None, **env: str) -> str:
        stamps = self.root / ".local" / "state" / "agent-context" / "health" / "bootstrap"
        stamps.mkdir(parents=True, exist_ok=True)
        (stamps / "s1").write_text("stamped\n")
        (self.root / "t.jsonl").write_text("a\nb\n")
        if record is not None:
            (self.state / "precompact").mkdir(parents=True, exist_ok=True)
            (self.state / "precompact" / "s1.json").write_text(json.dumps(record))
        payload = {"session_id": "s1", "transcript_path": str(self.root / "t.jsonl"),
                   "source": "compact"}
        proc = subprocess.run([sys.executable, str(DELIVER)], input=json.dumps(payload),
                              text=True, capture_output=True, env=self.env(**env))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.stamps = stamps
        out = json.loads(proc.stdout)
        return out["hookSpecificOutput"]["additionalContext"]

    @staticmethod
    def record(**extra: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "session": "s1", "trigger": "auto",
            "work": {"repo": "/x/proj", "branch": "wt-feature", "head": "abc1234",
                     "worktree": "feature", "modified": [" M src/app.py"],
                     "modified_count": 1},
            "store": {}}
        base.update(extra)
        return base

    
    def test_edited_files_and_asks_are_handed_back(self) -> None:
        ctx = self.deliver(self.record(
            edited_this_session=["/w/committed_then_clean.py"],
            recent_user_asks=["please fix the parser", "and add a test"]))
        for want in ("/w/committed_then_clean.py", "please fix the parser",
                     "and add a test", "src/app.py"):
            self.assertIn(want, ctx)

    
    def test_snapshot_is_framed_as_historical_after_the_directive(self) -> None:
        ctx = self.deliver(self.record(edited_this_session=["/w/a.py"],
                                       recent_user_asks=["do the thing"]))
        low = ctx.lower()
        self.assertIn("reference only", low)
        self.assertIn("do not re-run", low)
        self.assertLess(ctx.index(self.DIRECTIVE), low.index("reference only"))
        self.assertLess(ctx.index(self.DIRECTIVE), ctx.index("do the thing"))
        self.assertIn("BEFORE any other tool use", ctx)

    
    def big(self) -> dict[str, Any]:
        asks = ["ask %d %s" % (i, "y" * 150) for i in range(9)] + ["ZZLASTASK"]
        return self.record(edited_this_session=["/w/dir/file%d.py" % i for i in range(30)],
                           recent_user_asks=asks)

    def test_snapshot_is_truncated_at_the_cap_with_a_marker(self) -> None:
        ctx = self.deliver(self.big(), **{CAP_ENV: "400"})
        self.assertNotIn("ZZLASTASK", ctx)
        self.assertIn(CAP_ENV, ctx)
        self.assertIn(OFF_ENV, ctx)
        self.assertIn(self.DIRECTIVE, ctx)
        self.assertIn("BEFORE any other tool use", ctx)

    def test_default_cap_leaves_a_normal_snapshot_whole(self) -> None:
        ctx = self.deliver(self.big())
        self.assertIn("ZZLASTASK", ctx)
        self.assertNotIn("truncated", ctx.lower())

    def test_a_bad_cap_value_falls_back_to_the_default(self) -> None:
        for bad in ("abc", "0", "-5", ""):
            with self.subTest(value=bad):
                ctx = self.deliver(self.big(), **{CAP_ENV: bad})
                self.assertIn("ZZLASTASK", ctx)

    
    def test_off_switch_drops_the_snapshot_keeps_directive_and_stamp_expiry(self) -> None:
        for value in ("off", "OFF"):
            with self.subTest(value=value):
                ctx = self.deliver(self.record(edited_this_session=["/w/a.py"],
                                               recent_user_asks=["do the thing"]),
                                   **{OFF_ENV: value})
                self.assertNotIn("src/app.py", ctx)
                self.assertNotIn("/w/a.py", ctx)
                self.assertNotIn("do the thing", ctx)
                self.assertIn(self.DIRECTIVE, ctx)
                self.assertFalse((self.stamps / "s1").exists())
                self.assertEqual((self.stamps / "s1.compacted").read_text(), "2")

    
    def test_a_record_from_before_this_change_still_renders(self) -> None:
        ctx = self.deliver(self.record())
        self.assertIn("src/app.py", ctx)
        self.assertIn("WORKTREE: feature", ctx)

    def test_no_record_gives_the_bare_directive(self) -> None:
        ctx = self.deliver(None)
        self.assertIn(self.DIRECTIVE, ctx)
        self.assertIn("BEFORE any other tool use", ctx)
        self.assertNotIn("Historical", ctx)


if __name__ == "__main__":
    unittest.main()
