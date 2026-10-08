'The remote server suite receives the whole candidate tree its tests inspect.'

import importlib.util
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("verify-fleet.py")
spec = importlib.util.spec_from_file_location("verify_fleet_assets", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.org",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.org")


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], env=GIT_ENV, check=True,
                   capture_output=True)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class StageCandidateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        self.staged = Path(tmp.name) / "staged"
        write(self.store / ".gitignore", "server/.venv/\n")
        write(self.store / "projects/example-app/skills/a/SKILL.md", "skill\n")
        write(self.store / "global/scripts/entity-root-pages.py", "pass\n")
        write(self.store / "server/uv.lock", "lock\n")
        write(self.store / "server/src/mod.py", "committed\n")
        write(self.store / "server/tests/test_gone.py", "gone\n")
        git(self.store, "init", "-q")
        git(self.store, "add", "-A")
        git(self.store, "commit", "-q", "-m", "base")
        
        write(self.store / "server/src/mod.py", "edited\n")
        write(self.store / "server/tests/test_new.py", "new\n")
        (self.store / "server/tests/test_gone.py").unlink()
        write(self.store / "server/.venv/junk", "junk\n")
        self.staged.mkdir()
        module.stage_candidate(str(self.store), str(self.staged))

    def test_whole_tree_is_a_git_checkout(self):
        out = subprocess.run(["git", "-C", str(self.staged), "ls-files"], env=GIT_ENV,
                             check=True, capture_output=True, text=True).stdout.split()
        for rel in (".gitignore", "projects/example-app/skills/a/SKILL.md",
                    "global/scripts/entity-root-pages.py", "server/uv.lock"):
            self.assertIn(rel, out)
            self.assertTrue((self.staged / rel).exists(), rel)

    def test_working_tree_server_is_the_candidate(self):
        self.assertEqual((self.staged / "server/src/mod.py").read_text(), "edited\n")
        self.assertEqual((self.staged / "server/tests/test_new.py").read_text(), "new\n")
        self.assertFalse((self.staged / "server/tests/test_gone.py").exists())
        self.assertFalse((self.staged / "server/.venv").exists())


class RunOneTest(unittest.TestCase):
    def test_ships_the_staged_tree_and_syncs_the_verify_venv(self):
        copies = []

        def capture(_host, argv, _env, destination):
            copies.append((argv, destination))
            return 0

        with (patch.object(module, "ssh_ok", return_value=True),
              patch.object(module, "ssh_quiet"),
              patch.object(module, "tar_pipe_to_ssh", side_effect=capture),
              patch.object(module.subprocess, "run",
                           return_value=types.SimpleNamespace(stdout="RC=0\n")) as run):
            result = module.run_one("ls", "example.org", "/staged")

        self.assertEqual(result["rc"], "0")
        self.assertEqual(len(copies), 1)
        self.assertIn("/staged", copies[0][0])
        remote_cmd = run.call_args.args[0][-1]
        self.assertIn("uv sync -q --frozen --group dev", remote_cmd)
        self.assertNotIn(".agent-context/server/.venv", remote_cmd)

    def test_host_without_uv_is_skipped(self):
        with patch.object(module, "ssh_ok", side_effect=[True, False]):
            self.assertEqual(module.run_one("rp", "example.org", "/staged")["rc"], "NOUV")


class ReleaseServerTest(unittest.TestCase):
    'policy: a release has no fleet verification run, so release-server.py never\n    reaches this script, and its old skip flag is accepted and ignored.'

    RELEASE = SCRIPT.with_name("release-server.py")

    def run_release(self, *flags):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        
        
        env = dict(os.environ, AGENT_CONTEXT_STORE=tmp.name)
        return subprocess.run([sys.executable, str(self.RELEASE), *flags], env=env,
                              capture_output=True, text=True)

    def test_release_server_does_not_name_verify_fleet(self):
        self.assertNotIn("verify-fleet", self.RELEASE.read_text())

    def test_no_verify_is_accepted_and_ignored(self):
        proc = self.run_release("--no-verify")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("--no-verify does nothing", proc.stderr)
        self.assertIn("no agent-context store", proc.stderr)

    def test_unknown_flag_is_still_refused(self):
        proc = self.run_release("--bogus")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("unknown flag --bogus", proc.stderr)


if __name__ == "__main__":
    unittest.main()
