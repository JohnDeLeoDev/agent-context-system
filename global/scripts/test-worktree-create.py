#!/usr/bin/env python3
'Integration tests for the neutral worktree creation hook.'

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HOOK = Path(__file__).with_name("worktree-create.py")


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], text=True,
                          capture_output=True, check=True).stdout.strip()


class WorktreeCreateTest(unittest.TestCase):
    def setUp(self):
        scratch = Path.home() / ".cache" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="worktree-create-", dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "Test")
        git(self.root, "config", "user.email", "test@example.invalid")
        (self.root / "source.txt").write_text("source\n")
        git(self.root, "add", "source.txt")
        git(self.root, "commit", "-qm", "initial")

    def run_hook(self, name, cwd=None):
        return subprocess.run([sys.executable, str(HOOK)],
                              input=json.dumps({"name": name, "cwd": str(cwd or self.root)}),
                              text=True, capture_output=True)

    def test_new_worktree_uses_agents_directory(self):
        result = self.run_hook("feature-one")
        target = self.root / ".agents" / "worktrees" / "feature-one"
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(target))
        self.assertTrue((target / ".git").exists())
        self.assertEqual(git(target, "branch", "--show-current"), "feature-one")

    def test_existing_worktree_can_create_sibling(self):
        first = self.run_hook("first")
        self.assertEqual(first.returncode, 0, first.stderr)
        result = self.run_hook("second", self.root / ".agents" / "worktrees" / "first")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(),
                         str(self.root / ".agents" / "worktrees" / "second"))

    def test_main_checkout_edit_redirects_to_agents_worktree(self):
        import runpy
        first = self.run_hook("redirect")
        self.assertEqual(first.returncode, 0, first.stderr)
        state = Path(self.tmp.name) / "state"
        (state / "claims").mkdir(parents=True)
        (state / "claims" / "session.json").write_text(json.dumps({"worktree": "redirect"}))
        previous = os.environ.get("AGENT_CONTEXT_STATE_DIR")
        os.environ["AGENT_CONTEXT_STATE_DIR"] = str(state)
        try:
            guard = runpy.run_path(str(HOOK.parent.parent / "hooks" / "require-worktree-edit.py"))
            source = str(self.root / "new.py")
            result = guard["worktree_redirect"](
                {"session_id": "session", "tool_name": "Write",
                 "tool_input": {"file_path": source}}, str(self.root), source)
        finally:
            if previous is None:
                os.environ.pop("AGENT_CONTEXT_STATE_DIR", None)
            else:
                os.environ["AGENT_CONTEXT_STATE_DIR"] = previous
        target = str(self.root / ".agents" / "worktrees" / "redirect" / "new.py")
        self.assertEqual(result["hookSpecificOutput"]["updatedInput"]["file_path"], target)

    def test_rejects_path_traversal(self):
        result = self.run_hook("../escape")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root.parent / "escape").exists())
        self.assertEqual(result.stdout, "")

    def test_refuses_existing_path(self):
        first = self.run_hook("same")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_hook("same")
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(second.stdout, "")
        self.assertTrue((self.root / ".agents" / "worktrees" / "same" / ".git").exists())


if __name__ == "__main__":
    unittest.main()
