'test reexec drain.'
import threading
import time

import pytest

from agent_context import daemon, server


@pytest.fixture(autouse=True)
def _zero_inflight(monkeypatch):
    monkeypatch.setattr(daemon, "_INFLIGHT", 0)


def test_counter_enters_and_exits():
    daemon.inflight_enter()
    daemon.inflight_enter()
    assert daemon.inflight() == 2
    daemon.inflight_exit()
    assert daemon.inflight() == 1
    daemon.inflight_exit()
    daemon.inflight_exit()          
    assert daemon.inflight() == 0


def test_drain_returns_at_once_when_nothing_is_in_flight():
    t0 = time.monotonic()
    assert daemon.drain_inflight(timeout=5.0) is True
    assert time.monotonic() - t0 < 0.5


def test_drain_waits_for_a_call_to_finish():
    daemon.inflight_enter()

    def finish_later():
        time.sleep(0.2)
        daemon.inflight_exit()

    threading.Thread(target=finish_later, daemon=True).start()
    t0 = time.monotonic()
    assert daemon.drain_inflight(timeout=5.0) is True
    assert 0.15 <= time.monotonic() - t0 < 2.0


def test_drain_is_bounded_and_says_so(caplog):
    'A hung request must not pin a deploy forever: past the deadline the exec\n    proceeds and the drop is logged as a drop, not hidden.'
    daemon.inflight_enter()
    t0 = time.monotonic()
    assert daemon.drain_inflight(timeout=0.2) is False
    assert 0.15 <= time.monotonic() - t0 < 1.0
    assert "still in flight" in caplog.text


class _FakeApp:
    ' FakeApp.'

    def __init__(self):
        self.seen_inflight: list[int] = []

    async def __call__(self, scope, receive, send):
        self.seen_inflight.append(daemon.inflight())


_fake_app = _FakeApp()


@pytest.mark.parametrize("method,counted", [("POST", 1), ("DELETE", 1), ("GET", 0)])
def test_counting_app_counts_only_non_get_http(method, counted):
    "GET /mcp is the client's long-lived event stream. It never completes, so\n    counting it would make every drain run to its deadline."
    import anyio
    _fake_app.seen_inflight.clear()
    app = server._counting_app(_fake_app)
    anyio.run(app, {"type": "http", "method": method}, None, None)
    assert _fake_app.seen_inflight == [counted]
    assert daemon.inflight() == 0, "exited after the request, even on the GET path"


def test_counting_app_exits_even_when_the_app_raises():
    import anyio

    async def boom(scope, receive, send):
        raise RuntimeError("handler died")

    app = server._counting_app(boom)
    with pytest.raises(RuntimeError):
        anyio.run(app, {"type": "http", "method": "POST"}, None, None)
    assert daemon.inflight() == 0


def test_lifespan_and_websocket_scopes_are_not_counted():
    import anyio

    async def noop(scope, receive, send):
        _fake_app.seen_inflight.append(daemon.inflight())

    _fake_app.seen_inflight.clear()
    app = server._counting_app(noop)
    anyio.run(app, {"type": "lifespan"}, None, None)
    assert _fake_app.seen_inflight == [0]


class _RecordingLock:
    'An RLock that records each acquire, so the test can see the ORDER of events.'

    def __init__(self, order):
        self._lock = threading.RLock()
        self._order = order

    def acquire(self, *a, **k):
        self._order.append("lock")
        return self._lock.acquire(*a, **k)

    def release(self):
        self._lock.release()


def test_redeploy_drains_before_taking_the_store_lock(monkeypatch):
    'The order matters: a request that is waiting on the store lock can only finish\n    if the redeploy is not already holding it. Drain first, then lock, then exec.'
    order = []

    class Store:
        lock = _RecordingLock(order)

    
    monkeypatch.setattr(daemon, "_STARTED_VERSION", 100.0)
    monkeypatch.setattr(daemon, "_code_version", lambda: 200.0)
    monkeypatch.setattr(daemon, "_STARTED_FINGERPRINT", "booted")
    monkeypatch.setattr(daemon, "_code_fingerprint", lambda: "on-disk")
    monkeypatch.setattr(daemon, "_ATTEMPTED_VERSIONS", {})
    monkeypatch.delenv("AGENT_CONTEXT_SELF_DEPLOY", raising=False)
    monkeypatch.setattr(daemon, "run_gate", lambda *a, **k: (True, "gate passed"))
    monkeypatch.setattr(daemon, "_notify", lambda msg: None)
    monkeypatch.setattr(daemon, "_server_tree_dirty", lambda: False)
    monkeypatch.setattr(daemon, "drain_inflight",
                        lambda *a, **k: order.append("drain") or True)
    monkeypatch.setattr(daemon, "_do_exec", lambda: order.append("exec"))

    daemon.maybe_self_redeploy(Store())

    assert "exec" in order, order
    assert order.index("drain") < order.index("lock") < order.index("exec"), order
