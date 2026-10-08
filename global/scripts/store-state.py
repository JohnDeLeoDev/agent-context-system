#!/usr/bin/env python3
"store-state: the store's shared state files, read and changed only inside the daemon.\n\n  store-state.py claim-audit [--days N] [--stale-lock SECS] [--host NAME]\n      exit 0 and print `claimed` when the audit is overdue and no fresh lock is held, having\n      written running_since/running_host; exit 1 and print why not otherwise, including\n      when the state file does not exist.\n  store-state.py finish-audit\n      last_run = now, lock cleared.\n  store-state.py show-audit\n      print the state as JSON."
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store_task  

STATE_REL = os.path.join("global", "state", "context-audit.json")


def state_path():
    store = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")
    return os.path.join(store, STATE_REL)


def _int(value, default=0):
    s = str(value) if value is not None else ""
    return int(s) if s.isdigit() else default


def load():
    try:
        with open(state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(state):
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def claim_audit(days, stale_lock, host, now=None):
    now = int(time.time()) if now is None else now
    if not os.path.isfile(state_path()):
        
        return 1, "no state file at %s" % STATE_REL
    state = load()
    if now - _int(state.get("last_run")) <= days * 86400:
        return 1, "not due: last run %ds ago" % (now - _int(state.get("last_run")))
    running = _int(state.get("running_since"))
    if running > 0 and now - running <= stale_lock:
        return 1, "locked by %s since %ds ago" % (state.get("running_host") or "?", now - running)
    state["running_since"] = now
    state["running_host"] = host
    save(state)
    return 0, "claimed"


def finish_audit(now=None):
    state = load()
    state["last_run"] = int(time.time()) if now is None else now
    state["running_since"] = None
    state["running_host"] = None
    save(state)
    return 0, "finished"


def main():
    ap = argparse.ArgumentParser(prog="store-state.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim-audit")
    c.add_argument("--days", type=int, default=7)
    c.add_argument("--stale-lock", type=int, default=3600)
    c.add_argument("--host", default="unknown")
    sub.add_parser("finish-audit")
    sub.add_parser("show-audit")
    a = ap.parse_args()
    if a.cmd == "show-audit":
        print(json.dumps(load(), sort_keys=True))
        return 0
    code, text = (claim_audit(a.days, a.stale_lock, a.host) if a.cmd == "claim-audit"
                  else finish_audit())
    print(text)
    return code


if __name__ == "__main__":
    store_task.main_or_forward("store-state", main)
