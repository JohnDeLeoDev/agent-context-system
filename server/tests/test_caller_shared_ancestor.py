"Two Claude sessions in one tmux server share every process above their login shells: the\ntmux server, the service manager. `claims.caller` used to take the first claim whose\nrecorded pids met the caller's ancestry anywhere, so a write from one session was reported\nas another's (session 129220f7, in chezmoi). The owner is the claim at the caller's\nnearest process, and a process no nearer than OWNER_DEPTH above the peer names no one."
from agent_context import claims

TMUX, SYSTEMD = 2122, 900
CHEZMOI = {"session": "chezmoi", "started": 1.0, "pids": [110, 111, 112, TMUX, SYSTEMD]}
MINE = {"session": "mine", "started": 2.0, "pids": [210, 211, 212, TMUX, SYSTEMD]}


def _tree(monkeypatch, tree):
    monkeypatch.setattr(claims, "_ancestors", lambda pid, limit=8, **k: tree.get(pid, [])[:limit])


def test_the_nearest_process_wins_over_a_claim_sorted_first(monkeypatch):
    _tree(monkeypatch, {300: [211, 212, TMUX, SYSTEMD]})       
    assert claims.caller([CHEZMOI, MINE], 300)["session"] == "mine"


def test_a_process_whose_own_claim_is_gone_is_not_given_a_neighbour(monkeypatch):
    _tree(monkeypatch, {300: [211, 212, TMUX, SYSTEMD]})
    assert claims.caller([CHEZMOI], 300) is None


def test_a_claim_met_only_at_the_tmux_server_is_another_sessions(monkeypatch):
    
    
    _tree(monkeypatch, {320: [220, TMUX, SYSTEMD]})
    monkeypatch.setattr(claims, "_alive", lambda pid: pid in (400, 212))   
    pi = {"session": "pi", "started": 3.0, "pids": [400, TMUX, SYSTEMD]}
    assert claims.caller([pi, MINE], 320) is None
    _tree(monkeypatch, {410: [400, TMUX, SYSTEMD]})            
    assert claims.caller([pi, MINE], 410)["session"] == "pi"


def test_a_hook_shell_that_has_exited_does_not_hide_its_claim(monkeypatch):
    _tree(monkeypatch, {300: [212, TMUX, SYSTEMD]})            
    monkeypatch.setattr(claims, "_alive", lambda pid: pid == 212)
    assert claims.caller([CHEZMOI, MINE], 300)["session"] == "mine"


def test_the_newest_claim_on_one_claude_process_wins(monkeypatch):
    _tree(monkeypatch, {})
    cleared = {"session": "before-clear", "started": 1.0, "pids": [210, 211]}
    assert claims.caller([cleared, MINE], 211)["session"] == "mine"


def test_a_wrapper_shell_between_relay_and_claude_still_matches(monkeypatch):
    _tree(monkeypatch, {301: [250, 211, 212, TMUX]})           
    assert claims.caller([CHEZMOI, MINE], 301)["session"] == "mine"
