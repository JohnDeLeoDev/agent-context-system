'While a mirror was saturated (s2 under a btrfs scrub and two backup jobs, load 55), every\nMCP write committed and reset the push timer, so each write started another 30 s push and\nleft one more git-receive-pack stuck on the mirror. A commit pushes at once only while the\nlast push succeeded; during a backoff it waits for the backoff.'
from agent_context.commit_on_write import CommitOnWrite


class _Store:
    def __init__(self):
        self.commits = 0

    def commit_local(self, message):
        self.commits += 1
        return {"committed": True}


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _cow(push):
    clock = _Clock()
    cow = CommitOnWrite(_Store(), debounce=0.0, cap=0.0, clock=clock, push=push,
                        push_retry_secs=60.0)
    return cow, clock


def _write_and_commit(cow):
    cow.note_write(["global/memory/x.md"])
    cow.tick_commit()


def test_a_commit_during_a_backoff_waits_for_it():
    pushes = []
    cow, clock = _cow(lambda: pushes.append(clock.t) or {"push": False,
                                                          "push_error": {"s2": "timed out"}})
    _write_and_commit(cow)
    cow.tick_push()
    assert len(pushes) == 1
    for _ in range(5):                  
        clock.t += 5
        _write_and_commit(cow)
        cow.tick_push()
    assert len(pushes) == 1
    clock.t += 60
    cow.tick_push()
    assert len(pushes) == 2


def test_a_commit_after_a_good_push_pushes_at_once():
    pushes = []
    cow, clock = _cow(lambda: pushes.append(clock.t) or {"push": True})
    _write_and_commit(cow)
    cow.tick_push()
    clock.t += 1
    _write_and_commit(cow)
    cow.tick_push()
    assert len(pushes) == 2
