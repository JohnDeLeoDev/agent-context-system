"The daemon registers the store's merge drivers, and reports failures honestly."
import os
import subprocess
from pathlib import Path

import pytest
from fixture_signing import signing_config

from agent_context.store import ContextStore, _scrub_stale_git_hints



REAL_SCRIPT = "global/scripts/register-merge-drivers.py"


def _git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=check)


def _real_script(rootpath):
    'The shipped script, from this checkout if it has it, else the live store.\n\n    A worktree branched before the script landed does not carry it yet, and a test\n    that silently skipped there would be a test nobody notices has stopped running.'
    for base in (rootpath.parent, Path(os.path.expanduser("~/.agent-context"))):
        candidate = base / REAL_SCRIPT
        if candidate.exists():
            return candidate
    raise AssertionError(f"{REAL_SCRIPT} is missing from this checkout and the store")


@pytest.fixture
def store(tmp_path, request):
    src = _real_script(request.config.rootpath)
    root = tmp_path / "ctx"
    (root / "global" / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"),
                 *signing_config(tmp_path)):
        _git(root, "config", k, v)
    (root / REAL_SCRIPT).write_text(src.read_text())
    (root / "global" / "note.md").write_text("a memory\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return ContextStore(root=str(root)), root


def test_the_daemon_registers_every_driver_itself(store):
    s, root = store
    assert not _git(root, "config", "--get", "merge.acobservation.driver",
                    check=False).stdout.strip()

    s._register_merge_drivers()

    for key in ("acjsonunion", "acdocnewer", "acmetastamp", "acstatenewer",
                "acobservation"):
        driver = _git(root, "config", "--get", f"merge.{key}.driver",
                      check=False).stdout.strip()
        
        
        
        words = driver.split()
        assert len(words) > 1 and words[0].rsplit("/", 1)[-1].startswith("python") \
            and words[1].endswith(".py"), f"{key} not registered: {driver!r}"


def test_the_observation_attribute_is_written_too(store):
    'A driver with no attribute line is a driver git never consults.'
    s, root = store

    s._register_merge_drivers()

    attrs = (root / ".git" / "info" / "attributes").read_text()
    assert "global/audit-observations/*.json merge=acobservation" in attrs
    assert "global/state/*.json merge=acstatenewer" in attrs


def test_registration_is_idempotent(store):
    s, root = store

    s._register_merge_drivers()
    s._register_merge_drivers()

    attrs = (root / ".git" / "info" / "attributes").read_text()
    assert attrs.count("merge=acobservation") == 1


def test_a_missing_script_never_stops_a_sync(store):
    'Best effort by design: a machine that cannot register still merges.'
    s, root = store
    (root / REAL_SCRIPT).unlink()

    s._register_merge_drivers()   


def test_stale_stash_advice_is_stripped_from_a_failure_reason():
    raw = ("Auto-merging global/note.md\n"
           "CONFLICT (content): Merge conflict in global/note.md\n"
           "Created autostash: df585cb3\n"
           "Automatic merge failed; fix conflicts and then commit the result.\n"
           "When finished, apply stashed changes with `git stash pop`\n")

    out = _scrub_stale_git_hints(raw)

    assert "stash" not in out
    assert "CONFLICT (content): Merge conflict in global/note.md" in out
    assert "Automatic merge failed" in out


def test_the_environment_fault_text_survives_scrubbing():
    'test the environment fault text survives scrubbing.'
    assert "Cannot autostash" in _scrub_stale_git_hints("fatal: Cannot autostash\n")


def test_a_conflict_reason_carries_no_stash_instruction(monkeypatch, store):
    'End to end: what a human or an agent actually reads after a failed merge.'
    s, _root = store
    real = s._git

    class R:
        returncode = 1
        stdout = ""
        stderr = ("CONFLICT (content): Merge conflict in global/note.md\n"
                  "Created autostash: df585cb3\n"
                  "Automatic merge failed; fix conflicts and then commit the result.\n"
                  "When finished, apply stashed changes with `git stash pop`\n")

    def fake(*args, **kw):
        if args and args[0] == "merge" and "--abort" not in args:
            return R()
        return real(*args, **kw)

    monkeypatch.setattr(s, "_git", fake)
    monkeypatch.setattr(s, "_remote_mains", lambda b: (["origin"], ["origin/main"]))

    res = s.sync(push=False)

    assert res.get("pull") is False
    assert "stash" not in res.get("pull_error", "")
    assert "Merge conflict" in res.get("pull_error", "")
