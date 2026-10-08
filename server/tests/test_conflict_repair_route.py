'The reason text reaches a human through a phone alert and an agent through\nget_health, the SessionStart DEGRADED block and the per-turn sync-fault notice. It\nused to say "cd ~/.agent-context && git merge <base>", which guard-git-write refuses\nin the store\'s main checkout, so the only agent-reachable route was the one the guard\nnames instead, and that one rebased the branch: the other machine\'s commits landed as\ncopies, its tip never became an ancestor of main, and the daemon conflicted on the\nsame file every cycle while the landing had reported success.\n\nThese tests pin the shape of the advice, not its prose: no bare merge in the main\ncheckout, and the lander named.'
from agent_context.server import sync_verdict


def _reason(**res):
    return sync_verdict(res) or ""


def test_a_conflict_sends_the_reader_to_a_worktree():
    reason = _reason(pull=False, pull_error="CONFLICT in global/note.md",
                     behind=7, base="ls/main")

    assert "worktree add" in reason
    assert "store-wt-finish.py" in reason
    assert "merge ls/main" in reason


def test_a_conflict_never_tells_anyone_to_merge_in_the_main_checkout():
    '`cd ~/.agent-context && git merge` is refused by guard-git-write.'
    reason = _reason(pull=False, pull_error="CONFLICT in global/note.md",
                     behind=7, base="ls/main")

    assert "cd ~/.agent-context && git merge" not in reason


def test_the_branch_name_it_suggests_is_a_legal_ref():
    'A ref cannot carry the `/` in `ls/main`, so the suggestion has to be slugged.'
    reason = _reason(pull=False, pull_error="CONFLICT", behind=1, base="ls/main")

    assert "-b merge-ls-main" in reason
    assert "-b merge-ls/main" not in reason


def test_a_stranded_mirror_gets_the_same_route():
    reason = _reason(stranded_remotes=["s1/main", "s2/main"])

    assert "worktree add" in reason
    assert "store-wt-finish.py" in reason
    assert "cd ~/.agent-context && git merge" not in reason


def test_the_underlying_git_error_is_still_reported():
    'Naming a route must not cost the reader what actually went wrong.'
    reason = _reason(pull=False, pull_error="CONFLICT (content): global/note.md",
                     behind=2, base="origin/main")

    assert "CONFLICT (content): global/note.md" in reason
