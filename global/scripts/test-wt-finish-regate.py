#!/usr/bin/env python3
'The landing gate builds what lands (wt_finish_core._regate).\n\nEach test lands a branch from a throwaway repo with a bare origin, through a small\nproject file whose one gate is a shell "build": it fails when the file `uses` is present\nand `defined` is not, and counts its runs. Stdlib only.\n\nThe same day the writes gate failed three branches cut before a newer main, for files\nthey never touched: it compares the tree with CLASSIFIED, a list kept outside the repo\nthat describes main. A gate marked after_rebase now waits, when main moved, and judges\nthe rebased branch instead. WritesGateTests add such a gate: every `call-*` file in the\ntree must be listed in a classified file outside the repo, and every listed one present.\n\nRun directly: python3 test-wt-finish-regate.py'

import contextlib
import importlib.util
import io
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

CORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wt_finish_core.py")
CACHE_TMP = os.path.expanduser("~/.cache/tmp")

PROJECT_FILE = r'''
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("wt_finish_core", os.environ["TEST_CORE"])
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

BUILD = ('echo run >> "$TEST_COUNT"; echo "built $(git rev-parse HEAD)" >> "$TEST_COUNT.trees"; '
         'if [ -n "$TEST_ADVANCE" ] && [ ! -f "$TEST_ADVANCE.done" ]; then '
         'touch "$TEST_ADVANCE.done"; sh "$TEST_ADVANCE"; fi; '
         'if [ -f uses ] && [ ! -f defined ]; then echo "error: defined is gone"; exit 1; fi')

WRITES = ('echo run >> "$TEST_WRITES_COUNT"; bad=0; '
          'for f in call-*; do [ -f "$f" ] || continue; '
          'grep -qx "$f" "$TEST_CLASSIFIED" || { echo "  $f: never classified"; bad=1; }; done; '
          'while read -r f; do [ -f "$f" ] || { echo "  $f: classified but gone"; bad=1; }; '
          'done < "$TEST_CLASSIFIED"; exit $bad')

HANG = ('if [ -f other ]; then sleep 300 & echo "$$ $!" > "$TEST_PIDS"; '
        'echo "running HangingSuite.test_waits"; '
        'echo "Test Case \'-[Hang.Suite testWaits]\' started."; wait; fi')

def after_combine(ctx, d):
    # The post-rebase suite a project runs from this hook, under the lock but outside
    # the gates: TEST_SUITE names a shell script standing in for `swift test`.
    if os.environ.get("TEST_SUITE"):
        started = ((lambda proc: os.path.exists(os.environ["TEST_SLOT"]))
                   if os.environ.get("TEST_SLOT") else None)
        rc, output = core.run_deadline(["sh", os.environ["TEST_SUITE"]], PROJECT.gate_deadline,
                                       "post-combination suite", cwd=d, started=started)
        print(output)
        if rc != 0:
            core.die("post-combination suite failed, not landing")

def gates(ctx):
    gates = [core.Gate("build", lambda ctx: core.Cmd(["sh", "-c", BUILD], "build failed, not landing"),
                       "build gate")]
    if os.environ.get("TEST_QUEUE"):
        # Waits in line until TEST_SLOT exists, then works for TEST_QUEUE seconds.
        queued = ('while [ ! -f "$TEST_SLOT" ]; do sleep 0.2; done; echo "took the slot"; '
                  'sleep "$TEST_QUEUE"')
        gates.append(core.Gate("queued", lambda ctx: core.Cmd(["sh", "-c", queued],
                                                             "queued build failed, not landing"),
                               "queued build",
                               started=lambda proc: os.path.exists(os.environ["TEST_SLOT"])))
    if os.environ.get("TEST_HANG"):
        gates.append(core.Gate("suite", lambda ctx: core.Cmd(["sh", "-c", HANG], "suite failed, not landing"),
                               "test suite", parallel=os.environ["TEST_HANG"] == "parallel"))
    if os.environ.get("TEST_WRITES"):
        gates.insert(0, core.Gate("writes", lambda ctx: core.Cmd(["sh", "-c", WRITES],
                                                                 "writes gate failed, not landing"),
                                  "writes gate", after_rebase=os.environ["TEST_WRITES"] == "after"))
    return gates

PROJECT = core.Project("Test", push=core.Push("origin"), cleanup="hook", next_step=None,
                       landing_lock=os.environ["TEST_LOCK"] == "1",
                       rebase_first=os.environ.get("TEST_REBASE_FIRST") == "1",
                       gate_deadline=int(os.environ.get("TEST_DEADLINE", "600")))
sys.exit(core.run_project(globals(), PROJECT))
'''


class LandingFixture(unittest.TestCase):
    def setUp(self):
        os.makedirs(CACHE_TMP, exist_ok=True)
        self.base = tempfile.mkdtemp(dir=CACHE_TMP)
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        gitconfig = os.path.join(self.base, "gitconfig")
        open(gitconfig, "w").close()
        self.count = os.path.join(self.base, "count")
        self.project = os.path.join(self.base, "project.py")
        with open(self.project, "w", encoding="utf-8") as fh:
            fh.write(PROJECT_FILE)
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=gitconfig, GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
                        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com",
                        GATE_RELEVANCE=os.path.join(self.base, "no-gate-relevance"),
                        TEST_CORE=CORE, TEST_COUNT=self.count)
        self.bare = os.path.join(self.base, "origin.git")
        self.main = os.path.join(self.base, "main")
        self.git(self.base, "init", "-q", "--bare", "-b", "main", self.bare)
        self.git(self.base, "clone", "-q", self.bare, self.main)
        self.commit(self.main, "defined", "Add the definition")
        self.git(self.main, "push", "-q", "origin", "main")
        self.wt = os.path.join(self.main, ".claude", "worktrees", "feature")
        self.git(self.main, "worktree", "add", "-q", self.wt, "-b", "feature")
        with open(os.path.join(self.main, ".git", "info", "exclude"), "a") as fh:
            fh.write(".claude/\n")

    def git(self, cwd, *args):
        proc = subprocess.run(["git"] + list(args), cwd=cwd, env=self.env,
                              capture_output=True, text=True)
        assert proc.returncode == 0, "fixture: git %s\n%s" % (args, proc.stderr)
        return proc.stdout.strip()

    def commit(self, repo, name, message, remove=False):
        path = os.path.join(repo, name)
        if remove:
            os.remove(path)
        else:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(name + "\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", message)

    def advance_main(self, name, message, remove=False):
        'Another landing reaches origin after this branch forked.'
        self.commit(self.main, name, message, remove)
        self.git(self.main, "push", "-q", "origin", "main")

    def land(self, *args, lock=True, **extra_env):
        env = dict(self.env, TEST_LOCK="1" if lock else "0", **extra_env)
        return subprocess.run([sys.executable, self.project] + list(args), cwd=self.wt, env=env,
                              capture_output=True, text=True, timeout=120)

    def gate_runs(self, path=None):
        try:
            with open(path or self.count, encoding="utf-8") as fh:
                return len(fh.read().split())
        except FileNotFoundError:
            return 0


class RegateTests(LandingFixture):
    def test_branch_that_builds_alone_but_not_on_newer_main_is_refused(self):
        self.commit(self.wt, "uses", "Use the definition")
        self.advance_main("defined", "Remove the definition", remove=True)
        pushed = self.git(self.bare, "rev-parse", "main")
        proc = self.land()
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertEqual(self.gate_runs(), 2, out)
        self.assertIn("refusing to land", proc.stderr, out)
        self.assertIn("Remove the definition", proc.stderr, out)
        self.assertEqual(self.git(self.bare, "rev-parse", "main"), pushed,
                         "origin moved although the rebased branch failed its gate")
        self.assertEqual(self.git(self.main, "rev-parse", "main"), pushed,
                         "the main checkout moved although the rebased branch failed its gate")

    def test_unchanged_main_is_not_rebuilt(self):
        self.commit(self.wt, "uses", "Use the definition")
        proc = self.land()
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(self.gate_runs(), 1, out)
        self.assertIn("has not moved since the gate built this branch", proc.stdout, out)
        self.assertEqual(self.git(self.bare, "rev-parse", "main"),
                         self.git(self.wt, "rev-parse", "HEAD"))

    def test_main_that_moved_harmlessly_is_rebuilt_and_lands(self):
        self.commit(self.wt, "uses", "Use the definition")
        self.advance_main("other", "Add something unrelated")
        proc = self.land()
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(self.gate_runs(), 2, out)
        self.assertIn("gained 1 commit(s) the gate did not build", proc.stdout, out)
        self.assertEqual(self.git(self.bare, "rev-parse", "main"),
                         self.git(self.wt, "rev-parse", "HEAD"))

    def test_project_without_landing_lock_keeps_the_single_gate(self):
        self.commit(self.wt, "uses", "Use the definition")
        self.advance_main("defined", "Remove the definition", remove=True)
        proc = self.land(lock=False)
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(self.gate_runs(), 1, out)

    def test_no_gate_skips_the_second_build_too(self):
        self.commit(self.wt, "uses", "Use the definition")
        self.advance_main("other", "Add something unrelated")
        proc = self.land("--no-gate")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.gate_runs(), 0, proc.stdout + proc.stderr)


class WritesGateTests(LandingFixture):
    'A gate that compares the tree with a record of main judges the tree that lands.'

    def setUp(self):
        super().setUp()
        self.classified = os.path.join(self.base, "classified")
        self.writes_count = os.path.join(self.base, "writes-count")
        open(self.classified, "w").close()
        self.env.update(TEST_CLASSIFIED=self.classified, TEST_WRITES_COUNT=self.writes_count,
                        TEST_WRITES="after")

    def classify(self, name):
        with open(self.classified, "a", encoding="utf-8") as fh:
            fh.write(name + "\n")

    def writes_runs(self):
        return self.gate_runs(self.writes_count)

    def main_gains_a_classified_call(self):
        'Another landing adds a call and classifies it, after this branch was cut.'
        self.advance_main("call-main", "Add a classified call")
        self.classify("call-main")

    def test_stale_branch_that_did_not_touch_the_flagged_file_lands(self):
        self.commit(self.wt, "uses", "Use the definition")
        self.main_gains_a_classified_call()
        proc = self.land()
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(self.writes_runs(), 1, out)
        self.assertIn("so the writes gate waits to judge the rebased branch", proc.stdout, out)
        self.assertNotIn("call-main:", out)
        self.assertEqual(self.git(self.bare, "rev-parse", "main"),
                         self.git(self.wt, "rev-parse", "HEAD"))

    def test_without_the_opt_in_a_stale_branch_still_fails_early(self):
        
        self.commit(self.wt, "uses", "Use the definition")
        self.main_gains_a_classified_call()
        pushed = self.git(self.bare, "rev-parse", "main")
        proc = self.land(TEST_WRITES="early")
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("call-main: classified but gone", proc.stdout, out)
        self.assertEqual(self.git(self.bare, "rev-parse", "main"), pushed)

    def test_branch_that_adds_an_unclassified_call_still_fails(self):
        self.commit(self.wt, "call-branch", "Add a call nobody classified")
        self.main_gains_a_classified_call()
        pushed = self.git(self.bare, "rev-parse", "main")
        proc = self.land()
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertEqual(self.writes_runs(), 1, out)
        self.assertIn("call-branch: never classified", proc.stdout, out)
        self.assertNotIn("call-main:", out)
        self.assertIn("refusing to land: the rebased branch", proc.stderr, out)
        self.assertNotIn("passes its gate on its own base", proc.stderr, out)
        self.assertEqual(self.git(self.bare, "rev-parse", "main"), pushed)
        self.assertEqual(self.git(self.main, "rev-parse", "main"), pushed)

    def test_unclassified_call_on_a_current_branch_fails_early(self):
        self.commit(self.wt, "call-branch", "Add a call nobody classified")
        proc = self.land()
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertEqual(self.writes_runs(), 1, out)
        self.assertEqual(self.gate_runs(), 0, out)
        self.assertIn("writes gate failed, not landing", proc.stderr, out)

    def test_merge_mode_keeps_the_early_writes_gate(self):
        self.commit(self.wt, "uses", "Use the definition")
        self.main_gains_a_classified_call()
        proc = self.land("--merge")
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertNotIn("waits to judge", out)
        self.assertIn("call-main: classified but gone", proc.stdout, out)


class RebaseFirstTests(LandingFixture):
    'RebaseFirstTests.'

    def setUp(self):
        super().setUp()
        self.env.update(TEST_REBASE_FIRST="1")
        self.commit(self.wt, "uses", "Use the definition")

    def advance_while_gating(self, name, message, remove=False):
        'Another landing reaches main while the first gate builds.'
        script = os.path.join(self.base, "advance.sh")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write("set -e\ncd %s\n" % shlex.quote(self.main))
            if remove:
                fh.write("git rm -q %s\n" % shlex.quote(name))
            else:
                fh.write("echo %s > %s\ngit add %s\n" % ((shlex.quote(name),) * 3))
            fh.write("git commit -q -m %s\n" % shlex.quote(message))
        self.env.update(TEST_ADVANCE=script)

    def built_trees(self):
        with open(self.count + ".trees", encoding="utf-8") as fh:
            return [line.split()[1] for line in fh.read().splitlines()]

    def test_main_that_moved_before_the_landing_is_built_once(self):
        self.advance_main("other", "Add something unrelated")
        proc = self.land()
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(self.gate_runs(), 1, out)
        self.assertIn("has not moved since the gate built this branch", proc.stdout, out)
        landed = self.git(self.bare, "rev-parse", "main")
        self.assertEqual(landed, self.git(self.wt, "rev-parse", "HEAD"))
        self.assertEqual(self.built_trees(), [landed], "the gate did not build the tree that landed")

    def test_branch_broken_by_an_older_main_commit_fails_the_first_gate(self):
        self.advance_main("defined", "Remove the definition", remove=True)
        pushed = self.git(self.bare, "rev-parse", "main")
        before = self.git(self.wt, "rev-parse", "HEAD")
        proc = self.land()
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertEqual(self.gate_runs(), 1, out)
        self.assertIn("build failed, not landing", proc.stderr, out)
        self.assertIn("rebased onto main before the gates ran", proc.stderr, out)
        self.assertIn("git reset --hard %s" % before, proc.stderr, out)
        self.assertEqual(self.git(self.bare, "rev-parse", "main"), pushed)
        self.assertEqual(self.git(self.main, "rev-parse", "main"), pushed)

    def test_main_that_moves_while_gating_is_rebased_and_built_again(self):
        self.advance_while_gating("late", "Land something while this one gates")
        proc = self.land()
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(self.gate_runs(), 2, out)
        self.assertIn("gained 1 commit(s) the gate did not build", proc.stdout, out)
        landed = self.git(self.bare, "rev-parse", "main")
        self.assertEqual(self.built_trees()[-1], landed, "the re-gate did not build the tree that landed")

    def test_main_that_breaks_the_branch_while_gating_is_refused(self):
        self.advance_while_gating("defined", "Remove the definition", remove=True)
        pushed = self.git(self.bare, "rev-parse", "main")
        proc = self.land()
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertEqual(self.gate_runs(), 2, out)
        self.assertIn("refusing to land", proc.stderr, out)
        self.assertIn("Remove the definition", proc.stderr, out)
        self.assertEqual(self.git(self.bare, "rev-parse", "main"), pushed)

    def test_without_landing_lock_the_gates_build_the_branch_as_committed(self):
        before = self.git(self.wt, "rev-parse", "HEAD")
        self.advance_main("other", "Add something unrelated")
        proc = self.land(lock=False)
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(self.built_trees(), [before], out)

    def test_merge_mode_does_not_rebase(self):
        before = self.git(self.wt, "rev-parse", "HEAD")
        self.advance_main("other", "Add something unrelated")
        proc = self.land("--merge")
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(self.git(self.wt, "rev-parse", "HEAD"), before, out)
        self.assertNotIn("rebasing", proc.stdout, out)


class GateDeadlineTests(LandingFixture):
    'GateDeadlineTests.'

    def setUp(self):
        super().setUp()
        self.pids = os.path.join(self.base, "pids")
        self.lock = os.path.join(self.main, ".git", "wt-finish-landing.lock")
        self.env.update(TEST_PIDS=self.pids, TEST_DEADLINE="2")

    def assert_all_dead(self):
        with open(self.pids, encoding="utf-8") as fh:
            pids = [int(p) for p in fh.read().split()]
        self.assertEqual(len(pids), 2)
        for pid in pids:
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                self.fail("gate process %d outlived the deadline" % pid)

    def hang_under_the_lock(self, mode):
        self.commit(self.wt, "uses", "Use the definition")
        self.advance_main("other", "Add something unrelated")
        pushed = self.git(self.bare, "rev-parse", "main")
        started = time.monotonic()
        proc = self.land(TEST_HANG=mode)
        out = proc.stdout + proc.stderr
        self.assertLess(time.monotonic() - started, 60, out)
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("test suite ran past its 2s deadline and was killed", proc.stderr, out)
        self.assertIn("running HangingSuite.test_waits", proc.stdout, out)
        self.assertFalse(os.path.exists(self.lock), "the landing lock outlived the killed gate")
        self.assertEqual(self.git(self.bare, "rev-parse", "main"), pushed, out)
        self.assert_all_dead()

    def test_sequential_gate_past_its_deadline_is_killed_and_frees_the_lock(self):
        self.hang_under_the_lock("sequential")

    def test_parallel_gate_past_its_deadline_is_killed_and_frees_the_lock(self):
        self.hang_under_the_lock("parallel")

    def test_parallel_gate_names_the_test_still_running(self):
        
        self.commit(self.wt, "uses", "Use the definition")
        self.advance_main("other", "Add something unrelated")
        proc = self.land(TEST_HANG="parallel")
        self.assertIn("Still running when it was killed:\n  -[Hang.Suite testWaits]",
                      proc.stderr, proc.stdout + proc.stderr)

    def test_gate_inside_its_deadline_lands(self):
        self.commit(self.wt, "uses", "Use the definition")
        proc = self.land(TEST_HANG="sequential")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class QueuedGateTests(LandingFixture):
    'QueuedGateTests.'

    def setUp(self):
        super().setUp()
        self.slot = os.path.join(self.base, "slot")
        self.env.update(TEST_SLOT=self.slot, TEST_DEADLINE="2")
        self.commit(self.wt, "uses", "Use the definition")
        timer = threading.Timer(4, lambda: open(self.slot, "w").close())
        timer.start()
        self.addCleanup(timer.cancel)

    def test_waiting_in_line_past_the_deadline_lands(self):
        proc = self.land(TEST_QUEUE="0.5")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("took the slot", proc.stdout)

    def test_work_past_the_deadline_after_the_queue_is_killed(self):
        started = time.monotonic()
        proc = self.land(TEST_QUEUE="300")
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("queued build ran past its 2s deadline", proc.stderr, out)
        self.assertLess(time.monotonic() - started, 30, out)





SUITE_OUTPUT = """\
Test Suite 'All tests' started at 2026-09-26 22:38:09.971.
Test Case '-[KitTests.DoneTests testFinishes]' started.
Test Case '-[KitTests.DoneTests testFinishes]' passed (0.028 seconds).
Test Case '-[KitTests.FailTests testFails]' started.
Test Case '-[KitTests.FailTests testFails]' failed (0.100 seconds).
Test Case '-[KitTests.PoolTests testWaitsForever]' started.
\U001007c8  Test run started.
\U001007c8  Test "a quick one" started.
\U0010105b  Test "a quick one" passed after 0.001 seconds.
\U001007c8  Test "cases" started.
\U0010105b  Test "cases" with 2 test cases passed after 0.010 seconds.
\U001007c8  Test "a parked lock never returns" started.
\U00100884  Test "a parked lock never returns" recorded an issue at PoolTests.swift:9:5: Issue recorded
\U001007c8  Test unnamedCheck() started.
"""


class RunningTestsTests(unittest.TestCase):
    def test_names_only_the_tests_that_started_and_never_ended(self):
        spec = importlib.util.spec_from_file_location("wt_finish_core_under_test", CORE)
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        self.assertEqual(core.running_tests(SUITE_OUTPUT),
                         ["-[KitTests.PoolTests testWaitsForever]",
                          '"a parked lock never returns"', "unnamedCheck()"])
        self.assertEqual(core.running_tests(""), [])
        
        self.assertEqual(core.running_tests("Test basic() started.\nTest basic() started.\n"
                                            "Test basic() passed after 0.001 seconds.\n"),
                         ["basic()"])


class LockLineTests(unittest.TestCase):
    'LockLineTests.'

    def setUp(self):
        spec = importlib.util.spec_from_file_location("wt_finish_core_lock", CORE)
        self.core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.core)
        os.makedirs(CACHE_TMP, exist_ok=True)
        base = tempfile.mkdtemp(dir=CACHE_TMP)
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.path = os.path.join(base, "landing.lock")
        self.holder = subprocess.Popen(["sleep", "60"])
        self.addCleanup(self.holder.kill)
        os.mkdir(self.path)
        with open(os.path.join(self.path, "pid"), "w") as fh:
            fh.write(str(self.holder.pid))
        os.makedirs(self.path + ".waiting")
        open(os.path.join(self.path + ".waiting", str(os.getppid())), "w").close()
        open(os.path.join(self.path + ".waiting", "999999"), "w").close()  

    def take(self):
        lock = self.core._Lock(self.path)
        lock.WAIT, lock.NOTE_EVERY = 6, 2
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                lock.take()
            except self.core.Died as e:
                return lock, out.getvalue(), e.msg
        return lock, out.getvalue(), None

    def test_a_waiter_names_the_holder_and_the_line_then_gives_up(self):
        _lock, out, died = self.take()
        self.assertIn("pid %d has held it for" % self.holder.pid, out)
        self.assertIn("and 1 other landing(s) wait too", out)
        self.assertIn("waited 0 minutes for the landing lock", died or "")
        self.assertEqual(os.listdir(self.path + ".waiting"), [str(os.getppid())])

    def test_a_dead_holder_is_still_broken_at_once(self):
        self.holder.kill()
        self.holder.wait()
        lock, _out, died = self.take()
        self.assertIsNone(died)
        self.assertTrue(lock.held)
        lock.release()


class HookSuiteDeadlineTests(LandingFixture):
    'HookSuiteDeadlineTests.'

    def setUp(self):
        super().setUp()
        self.pids = os.path.join(self.base, "pids")
        self.lock = os.path.join(self.main, ".git", "wt-finish-landing.lock")
        self.suite = os.path.join(self.base, "suite.sh")
        self.env.update(TEST_PIDS=self.pids, TEST_DEADLINE="2", TEST_SUITE=self.suite)
        self.commit(self.wt, "uses", "Use the definition")
        self.advance_main("other", "Add something unrelated")

    def write_suite(self, hang):
        lines = "".join("printf '%%s\\n' %s\n" % shlex.quote(line)
                        for line in SUITE_OUTPUT.splitlines())
        with open(self.suite, "w", encoding="utf-8") as fh:
            fh.write(lines)
            if hang:
                fh.write('sleep 300 & echo "$$ $!" > "$TEST_PIDS"; wait\n')

    def test_hung_suite_is_killed_names_its_running_tests_and_frees_the_lock(self):
        self.write_suite(hang=True)
        pushed = self.git(self.bare, "rev-parse", "main")
        started = time.monotonic()
        proc = self.land()
        out = proc.stdout + proc.stderr
        self.assertLess(time.monotonic() - started, 60, out)
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("post-combination suite ran past its 2s deadline and was killed", proc.stderr, out)
        self.assertIn("Still running when it was killed:\n"
                      "  -[KitTests.PoolTests testWaitsForever]\n"
                      '  "a parked lock never returns"\n'
                      "  unnamedCheck()\n", proc.stderr, out)
        self.assertNotIn("testFinishes]\n", proc.stderr, out)
        self.assertIn("Test unnamedCheck() started.", proc.stdout, out)
        self.assertFalse(os.path.exists(self.lock), "the landing lock outlived the killed suite")
        self.assertEqual(self.git(self.bare, "rev-parse", "main"), pushed, out)
        GateDeadlineTests.assert_all_dead(self)

    def test_suite_inside_its_deadline_lands(self):
        self.write_suite(hang=False)
        proc = self.land()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.git(self.bare, "rev-parse", "main"),
                         self.git(self.wt, "rev-parse", "HEAD"))


class QueuedHookSuiteTests(HookSuiteDeadlineTests):
    'QueuedHookSuiteTests.'

    def setUp(self):
        super().setUp()
        self.slot = os.path.join(self.base, "slot")
        self.env.update(TEST_SLOT=self.slot)
        timer = threading.Timer(4, lambda: open(self.slot, "w").close())
        timer.start()
        self.addCleanup(timer.cancel)

    def write_suite(self, hang):
        super().write_suite(hang)
        with open(self.suite, encoding="utf-8") as fh:
            body = fh.read()
        with open(self.suite, "w", encoding="utf-8") as fh:
            fh.write('while [ ! -f "$TEST_SLOT" ]; do sleep 0.2; done\n' + body)


class NamedGateSelectionTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("wt_finish_core_selection", CORE)
        self.core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.core)

    def test_selected_gate_pulls_its_dependency_and_required_gate(self):
        ran = []
        gates = [self.core.Gate("build", lambda ctx: ran.append("build") or
                                self.core.Cmd(["true"], "build failed"), "build"),
                 self.core.Gate("tests", lambda ctx: ran.append("tests") or
                                self.core.Cmd(["true"], "tests failed"), "tests",
                                requires=("build",)),
                 self.core.Gate("deploy", lambda ctx: ran.append("deploy") or
                                self.core.Cmd(["true"], "deploy failed"), "deploy",
                                required=True)]
        ctx = type("Ctx", (), {"opts": {"checks": ("tests",), "gate": True},
                                "hook": lambda self, name: (lambda ignored: gates),
                                "project": type("P", (), {"landing_lock": False,
                                                           "gate_deadline": None})(),
                                "mode": "rebase", "worktree": os.getcwd(), "failed_gate": "",
                                "deferred": []})()
        original = self.core._relevance_skips
        self.core._relevance_skips = lambda ignored: False
        self.addCleanup(setattr, self.core, "_relevance_skips", original)
        self.core._gates(ctx)
        self.assertEqual(ran, ["build", "tests", "deploy"])

    def test_required_gate_survives_relevance_skip_and_check_none(self):
        ran = []
        gates = [self.core.Gate("build", lambda ctx: ran.append("build"), "build"),
                 self.core.Gate("vendor", lambda ctx: ran.append("vendor") or
                                self.core.Cmd(["true"], "vendor failed"), "vendor",
                                required=True)]
        ctx = type("Ctx", (), {"opts": {"checks": ("none",), "gate": False},
                                "hook": lambda self, name: (lambda ignored: gates),
                                "project": type("P", (), {"landing_lock": False,
                                                           "gate_deadline": None})(),
                                "mode": "rebase", "worktree": os.getcwd(), "failed_gate": "",
                                "deferred": []})()
        original = self.core._relevance_skips
        self.core._relevance_skips = lambda ignored: True
        self.addCleanup(setattr, self.core, "_relevance_skips", original)
        self.core._gates(ctx)
        self.assertEqual(ran, ["vendor"])

    def test_unknown_named_gate_is_refused(self):
        gates = [self.core.Gate("build", lambda ctx: None, "build")]
        ctx = type("Ctx", (), {"opts": {"checks": ("missing",), "gate": True},
                                "hook": lambda self, name: (lambda ignored: gates),
                                "project": type("P", (), {"landing_lock": False,
                                                           "gate_deadline": None})(),
                                "mode": "rebase", "failed_gate": "", "deferred": []})()
        with self.assertRaisesRegex(self.core.Died, "unknown gate"):
            self.core._gates(ctx)

    def test_special_selector_cannot_be_combined(self):
        gates = [self.core.Gate("build", lambda ctx: None, "build")]
        for checks, message in ((("all", "build"), "all"), (("none", "build"), "none")):
            ctx = type("Ctx", (), {"opts": {"checks": checks, "gate": True},
                                    "hook": lambda self, name: (lambda ignored: gates),
                                    "project": type("P", (), {"landing_lock": False,
                                                               "gate_deadline": None})(),
                                    "mode": "rebase", "failed_gate": "", "deferred": []})()
            with self.assertRaisesRegex(self.core.Died, message):
                self.core._gates(ctx)

    def test_selected_gate_cannot_be_silently_skipped(self):
        gates = [self.core.Gate("tests", lambda ctx: None, "tests")]
        ctx = type("Ctx", (), {"opts": {"checks": ("tests",), "gate": True},
                                "hook": lambda self, name: (lambda ignored: gates),
                                "project": type("P", (), {"landing_lock": False,
                                                           "gate_deadline": None})(),
                                "mode": "rebase", "failed_gate": "", "deferred": []})()
        original = self.core._relevance_skips
        self.core._relevance_skips = lambda ignored: False
        self.addCleanup(setattr, self.core, "_relevance_skips", original)
        with self.assertRaisesRegex(self.core.Died, "selected gate tests did not run"):
            self.core._gates(ctx)

    def test_selected_gate_cannot_be_combined_with_no_gate(self):
        gates = [self.core.Gate("tests", lambda ctx: None, "tests")]
        ctx = type("Ctx", (), {"opts": {"checks": ("tests",), "gate": False},
                                "hook": lambda self, name: (lambda ignored: gates),
                                "project": type("P", (), {"landing_lock": False,
                                                           "gate_deadline": None})(),
                                "mode": "rebase", "failed_gate": "", "deferred": []})()
        with self.assertRaisesRegex(self.core.Died, "--no-gate"):
            self.core._gates(ctx)

    def test_check_all_cannot_be_combined_with_no_gate(self):
        gates = [self.core.Gate("tests", lambda ctx: None, "tests")]
        ctx = type("Ctx", (), {"opts": {"checks": ("all",), "gate": False},
                                "hook": lambda self, name: (lambda ignored: gates),
                                "project": type("P", (), {"landing_lock": False,
                                                           "gate_deadline": None})(),
                                "mode": "rebase", "failed_gate": "", "deferred": []})()
        with self.assertRaisesRegex(self.core.Died, "--no-gate"):
            self.core._gates(ctx)

    def test_check_all_cannot_silently_skip_a_gate(self):
        gates = [self.core.Gate("tests", lambda ctx: None, "tests")]
        ctx = type("Ctx", (), {"opts": {"checks": ("all",), "gate": True},
                                "hook": lambda self, name: (lambda ignored: gates),
                                "project": type("P", (), {"landing_lock": False,
                                                           "gate_deadline": None})(),
                                "mode": "rebase", "failed_gate": "", "deferred": []})()
        original = self.core._relevance_skips
        self.core._relevance_skips = lambda ignored: False
        self.addCleanup(setattr, self.core, "_relevance_skips", original)
        with self.assertRaisesRegex(self.core.Died, "selected gate tests did not run"):
            self.core._gates(ctx)

    def test_required_gate_is_not_dropped_when_regating_deferred_checks(self):
        ran = []
        gates = [self.core.Gate("build", lambda ctx: ran.append("build"), "build"),
                 self.core.Gate("tests", lambda ctx: ran.append("tests"), "tests",
                                after_rebase=True),
                 self.core.Gate("vendor", lambda ctx: ran.append("vendor") or
                                self.core.Cmd(["true"], "vendor failed"), "vendor",
                                required=True)]
        ctx = type("Ctx", (), {"opts": {"checks": ("tests",), "gate": True},
                                "hook": lambda self, name: (lambda ignored: gates),
                                "project": type("P", (), {"landing_lock": True,
                                                           "gate_deadline": None})(),
                                "mode": "rebase", "target": "main", "worktree": os.getcwd(),
                                "failed_gate": "",
                                "deferred": []})()
        original = self.core._relevance_skips
        self.core._relevance_skips = lambda ignored: False
        self.addCleanup(setattr, self.core, "_relevance_skips", original)
        self.core._gates(ctx, only=("tests",), stale=1)
        self.assertEqual(ran, ["vendor"])
        self.assertEqual(ctx.deferred, ["tests"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
