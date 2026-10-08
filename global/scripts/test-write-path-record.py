#!/usr/bin/env python3
'Run directly: python3 test-write-path-record.py'

import importlib.util
import json
import os
import sys
import unittest

_spec = importlib.util.spec_from_file_location(
    "agentorchestra_script_fixes",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "test-agentorchestra-script-fixes.py"))
fixes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixes)

CALL = "try await ConnectionPool.withSession(host) { _ in }\n"


class RecordInTheTreeTests(fixes.TempDirMixin, unittest.TestCase):
    def write(self, root, rel, text):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def record(self, root, files):
        self.write(root, "ci/write-paths.json", json.dumps({
            "about": "test", "files": {rel: {"calls": n, "reason": "a read"} for rel, n in files.items()}}))

    def check(self, root, *args):
        return fixes.run([sys.executable, fixes.script("write-path-check.py"), root] + list(args))

    def test_the_trees_own_record_classifies_its_calls(self):
        root = self.mkdtemp()
        self.write(root, "iOS/Models/Reader.swift", CALL + CALL)
        self.record(root, {"iOS/Models/Reader.swift": 2})
        proc = self.check(root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("All 2 ConnectionPool.withSession calls in 1 files", proc.stdout)

    def test_a_tree_without_a_record_fails_and_says_why(self):
        root = self.mkdtemp()
        self.write(root, "iOS/Models/Reader.swift", CALL)
        proc = self.check(root)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("no ci/write-paths.json in", proc.stdout)
        self.assertIn("iOS/Models/Reader.swift: 1 call(s) in a file this check has never classified",
                      proc.stdout)

    def test_the_counting_rules_read_the_record(self):
        root = self.mkdtemp()
        self.write(root, "iOS/Models/Fewer.swift", CALL)
        self.write(root, "iOS/Models/More.swift", CALL + CALL)
        self.record(root, {"iOS/Models/Fewer.swift": 2, "iOS/Models/More.swift": 1,
                           "iOS/Models/Gone.swift": 1})
        proc = self.check(root)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("Fewer.swift: 1 calls, 2 classified: lower the recorded count", proc.stdout)
        self.assertIn("More.swift: 2 calls, 1 classified: a NEW withSession call", proc.stdout)
        self.assertIn("Gone.swift: 0 calls, 1 classified: remove the entry", proc.stdout)

    def test_a_branch_classifying_its_new_file_leaves_other_branches_alone(self):
        
        
        root = fixes.make_git_repo(self.mkdtemp())
        self.write(root, "iOS/Models/Old.swift", CALL)
        self.record(root, {"iOS/Models/Old.swift": 1})
        fixes.commit_all(root, "main")
        fixes.setup_ok(["git", "-C", root, "checkout", "-q", "-b", "adds-a-file"])
        self.write(root, "iOS/Models/New.swift", CALL)
        self.record(root, {"iOS/Models/Old.swift": 1, "iOS/Models/New.swift": 1})
        fixes.commit_all(root, "a new read, classified in the same commit")
        proc = self.check(root, "--base", "main")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        fixes.setup_ok(["git", "-C", root, "checkout", "-q", "-b", "other", "main"])
        self.write(root, "iOS/Models/Unrelated.swift", "// no calls\n")
        fixes.commit_all(root, "another branch, cut from main")
        proc = self.check(root, "--base", "main")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
