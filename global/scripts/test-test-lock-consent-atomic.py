'A failed unlock audit write must leave the test locked.'

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent


class ConsentAtomicityTests(unittest.TestCase):
    def test_log_failure_keeps_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            repo = base / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            target = repo / "locked_test.py"
            target.write_text("assert True\n")
            env = dict(os.environ, XDG_STATE_HOME=str(base / "state"))
            lock = SCRIPTS / "test-lock.py"
            consent = SCRIPTS / "test-lock-consent.py"
            env["TEST_LOCK_TOOL"] = str(lock)
            subprocess.run([sys.executable, str(lock), "lock", str(target)],
                           env=env, check=True, capture_output=True, text=True)
            log = base / "state/agent-context/test-lock-consent.log"
            log.mkdir()
            result = subprocess.run([sys.executable, str(consent), str(target)],
                                    env=env, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            status = subprocess.run([sys.executable, str(lock), "status", str(repo)],
                                    env=env, capture_output=True, text=True, check=False)
            self.assertIn("locked_test.py", status.stdout)


if __name__ == "__main__":
    unittest.main()
