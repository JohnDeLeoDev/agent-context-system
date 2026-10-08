"These pin the watchdog's decision rule rather than the relay plumbing: it must wait\nwhile the parent is unchanged, and return as soon as it is not, whatever the new\nppid happens to be."
import anyio
import pytest

from agent_context import daemon


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    'Real interval is 5s; nothing here should spend it.'
    monkeypatch.setattr(daemon, "_PARENT_POLL_SECONDS", 0.001)


def test_returns_when_the_parent_is_gone(monkeypatch):
    'Reparented to init — the case that actually happens when a session dies.'
    seen = iter([4242, 4242, 4242, 1])
    monkeypatch.setattr(daemon.os, "getppid", lambda: next(seen))

    async def scenario():
        with anyio.fail_after(5):        
            await daemon._await_parent_exit(4242)

    anyio.run(scenario)


def test_returns_on_any_ppid_change_not_just_init(monkeypatch):
    'The check is against the STARTING ppid, so a re-parent to a subreaper counts.\n\n    Linux subreapers (and container init shims) adopt orphans instead of init, so a\n    watchdog written as `while getppid() != 1` would wait forever exactly where the\n    process supervision is most careful.'
    seen = iter([4242, 9999])
    monkeypatch.setattr(daemon.os, "getppid", lambda: next(seen))

    async def scenario():
        with anyio.fail_after(5):
            await daemon._await_parent_exit(4242)

    anyio.run(scenario)


def test_waits_while_the_parent_is_alive(monkeypatch):
    'A live client must never be torn down — this is the expensive false positive.'
    monkeypatch.setattr(daemon.os, "getppid", lambda: 4242)

    async def scenario():
        with anyio.move_on_after(0.05) as scope:
            await daemon._await_parent_exit(4242)
        return scope.cancelled_caught

    assert anyio.run(scenario) is True, "watchdog returned while the parent was alive"


def test_bridge_starts_the_watchdog():
    'The helper is only useful if the bridge actually runs it.'
    import inspect
    src = inspect.getsource(daemon._bridge)
    assert "watch_parent" in src
    assert "tg.start_soon(watch_parent)" in src
    assert "_await_parent_exit" in src
