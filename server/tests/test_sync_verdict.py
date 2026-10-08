'What a completed sync() cycle means for the liveness heartbeat (server.sync_verdict).'
from agent_context import server


def test_clean_cycle_is_healthy():
    assert server.sync_verdict({"pull": True, "push": {}}) is None


def test_conflicting_rebase_is_a_failure():
    reason = server.sync_verdict({
        "pull": False,
        "pull_error": "CONFLICT (content): Merge conflict in server/VERSION",
        "base": "origin/main",
        "behind": 86,
    })
    assert reason is not None
    assert "86 commit(s) behind origin/main" in reason
    assert "CONFLICT" in reason


def test_conflicting_rebase_without_a_count_still_fails():
    '`behind` is best-effort (a rev-list that itself failed leaves it absent) — the\n    verdict must not depend on it, only the wording.'
    reason = server.sync_verdict({"pull": False, "pull_error": "fatal: needs a merge"})
    assert reason is not None
    assert "behind" not in reason
    assert "needs a merge" in reason


def test_deferred_integration_is_tolerated_then_reported():
    'test deferred integration is tolerated then reported.'
    below = {"pull": False, "pull_skipped": "uncommitted server/ edits (policy)",
             "pull_skip_streak": server._DEFER_STALL_CYCLES - 1}
    assert server.sync_verdict(below) is None

    at_limit = {**below, "pull_skip_streak": server._DEFER_STALL_CYCLES}
    reason = server.sync_verdict(at_limit)
    assert reason is not None
    assert str(server._DEFER_STALL_CYCLES) in reason


def test_refused_push_is_tolerated_briefly_then_reported():
    'test refused push is tolerated briefly then reported.'
    refused = {"push": False, "pull": True,
               "push_error": {"ls": "! [remote rejected] main (pre-receive hook declined)",
                              "s1": "! [remote rejected] main (pre-receive hook declined)"}}
    assert server.sync_verdict({**refused, "push_fail_streak": 1}) is None
    assert server.sync_verdict({**refused,
                                "push_fail_streak": server._PUSH_STALL_CYCLES - 1}) is None

    reason = server.sync_verdict({**refused,
                                  "push_fail_streak": server._PUSH_STALL_CYCLES})
    assert reason is not None
    assert "2 remote(s)" in reason
    assert "pre-receive hook declined" in reason   
    assert "ls" in reason and "s1" in reason


def test_refused_push_quotes_the_rejection_not_the_trailing_hint():
    'test refused push quotes the rejection not the trailing hint.'
    err = ("To example.invalid:/home/user/git-server/agent-context\n"
           " ! [remote rejected] main (unpacker error)\n"
           "error: failed to push some refs\n"
           "hint: See the 'Note about fast-forwards' in 'git push --help' for details.\n")
    reason = server.sync_verdict({"push": False, "pull": True, "push_error": {"ls": err},
                                  "push_fail_streak": server._PUSH_STALL_CYCLES})
    assert "unpacker error" in reason
    assert "fast-forwards" not in reason


def test_local_corruption_is_reported_immediately_with_the_remedy():
    res = {"push": False, "pull": True, "push_fail_streak": 1,
           "push_error": {"ls": " ! [remote rejected] main (unpacker error)"},
           "local_corruption": {"count": 2, "empty_objects": ["5e/248b7a", "af/cd0576"]}}
    reason = server.sync_verdict(res)
    assert reason is not None
    assert "2 empty loose object" in reason and "5e/248b7a" in reason
    assert "git fetch" in reason


def test_a_push_that_lands_is_healthy_even_after_earlier_failures():
    assert server.sync_verdict({"pull": True, "push": True, "push_fail_streak": 0}) is None


def test_truncated_tracked_file_is_reported_immediately():
    'test truncated tracked file is reported immediately.'
    res = {"pull": False, "commit_skipped": "tracked file(s) truncated to 0 bytes",
           "truncated": ["global/scripts/materialize.sh"],
           "truncated_hold": ["global/scripts/materialize.sh"]}
    reason = server.sync_verdict(res)
    assert reason is not None
    assert "REFUSED" in reason
    assert "global/scripts/materialize.sh" in reason
    assert "policy" in reason


def test_truncated_hold_wins_over_every_later_signal():
    'It must be judged FIRST: a stale behind_streak or push streak from before the\n    wedge would otherwise name the wrong remedy.'
    res = {"pull": False, "truncated_hold": ["x.md"], "behind_streak": 99,
           "push_fail_streak": 99, "push_error": {"ls": "stale"}}
    assert "REFUSED" in server.sync_verdict(res)


def test_no_remotes_is_healthy():
    'A store with nothing to integrate syncs successfully — pull True, no error.'
    assert server.sync_verdict({"pull": True}) is None
    assert server.sync_verdict({}) is None
