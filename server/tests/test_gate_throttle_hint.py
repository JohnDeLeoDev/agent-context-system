'The signal that separates the two is cheap and unambiguous: a hung test burns wall\ntime with the CPU busy or blocked on one syscall; a held run burns wall time having\nbeen given almost no CPU at all.'
import pytest

from agent_context.daemon import _throttle_hint


HELD = {"cpu": 17.0, "wall": 263.0}


@pytest.fixture
def on_macos(monkeypatch):
    monkeypatch.setattr("agent_context.daemon.sys.platform", "darwin")


@pytest.fixture
def on_linux(monkeypatch):
    monkeypatch.setattr("agent_context.daemon.sys.platform", "linux")


def test_almost_no_cpu_across_a_long_wall_is_reported_as_waiting(on_macos):
    hint = _throttle_hint(**HELD)
    assert hint
    assert "WAITING" in hint
    assert "not a hung test" in hint


def test_macos_gets_the_qos_remedy(on_macos):
    hint = _throttle_hint(**HELD)
    assert "ProcessType" in hint


def test_the_macos_hint_names_adaptive_as_insufficient(on_macos):
    'test the macos hint names adaptive as insufficient.'
    hint = _throttle_hint(**HELD)
    assert "Adaptive" in hint and "Standard" in hint


def test_linux_is_never_told_to_check_a_launchagent(on_linux):
    'test linux is never told to check a launchagent.'
    hint = _throttle_hint(**HELD)
    assert hint
    assert "ProcessType" not in hint
    assert "LaunchAgent" not in hint
    assert "Adaptive" not in hint


def test_linux_gets_the_blocking_io_remedy(on_linux):
    'test linux gets the blocking io remedy.'
    hint = _throttle_hint(**HELD)
    assert "TMPDIR" in hint and "fsync" in hint


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_the_shape_line_is_the_same_on_every_platform(monkeypatch, platform):
    "Only the remedy is per-platform. The measurement that PROVES the gate was\n    held has to read identically everywhere, or two machines' alerts cannot be\n    compared — and an unrecognized platform must still get the measurement plus a\n    remedy, never an empty hint."
    monkeypatch.setattr("agent_context.daemon.sys.platform", platform)
    hint = _throttle_hint(**HELD)
    assert hint.startswith("used only 17s CPU across 263s wall (6%) — the gate was WAITING")
    assert len(hint) > 120


def test_a_genuinely_busy_timeout_is_not_blamed_on_waiting(on_macos):
    "A test spinning on the CPU for the whole gate IS a hung test. Saying\n    'waiting' there would send the next reader down the wrong path in exactly\n    the way this function exists to prevent."
    assert _throttle_hint(cpu=230.0, wall=263.0) == ""


def test_the_boundary_is_a_quarter_of_the_wall(on_macos):
    assert _throttle_hint(cpu=26.0, wall=100.0) == ""      
    assert _throttle_hint(cpu=24.0, wall=100.0)            


def test_unmeasurable_cpu_says_nothing_rather_than_guessing(on_macos):
    "_child_cpu_secs returns 0.0 where the platform cannot report it. Zero must\n    not be read as 'no CPU used', which would make every timeout claim throttling."
    assert _throttle_hint(cpu=0.0, wall=263.0) == ""
    assert _throttle_hint(cpu=17.0, wall=0.0) == ""
