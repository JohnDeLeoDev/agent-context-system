#!/usr/bin/env python3
'A script that reads or rewrites the store tree is a server task. The daemon runs it with\nAGENT_CONTEXT_SERVER_TASK=1 through the run_store_task MCP tool; started anywhere else\n(a shell, a hook, a relay machine), it hands its argv to that tool and prints what the\ndaemon\'s run printed, with the same exit code. So the script opens store files only inside\nthe daemon, and every caller keeps running it the way it always did.\n\n    if __name__ == "__main__":\n        store_task.main_or_forward("invariant-check", main)\n\n`main` takes no arguments and returns an exit code (read sys.argv yourself). Stdin is\npassed along only when the task asks for it (`stdin=True`) and stdin is not a terminal.\nExit 3 means the store did not answer; the message names why.\n\nONLY THE LIVE STORE FORWARDS. The rule covers agent-context content, which lives in the live\nstore (~/.agent-context on ls, served by the daemon). A test battery points a script at a\nfixture tree through AGENT_CONTEXT_STORE (or the script\'s own flag, passed as `store`); that\ntree is not the store, so the script runs on it locally, exactly as before.'
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store_mcp  

SERVER_TASK_ENV = "AGENT_CONTEXT_SERVER_TASK"
UNREACHABLE_EXIT = 3


def in_server():
    "True inside a run_store_task run: the store tree here is the daemon's own."
    return os.environ.get(SERVER_TASK_ENV) == "1"


def live_store():
    "The account's own store path, from its passwd home: a HOME pointed at a fixture\n    tree (every locked battery does this) does not move the live store."
    try:
        import pwd
        home = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):
        home = os.path.expanduser("~")
    return os.path.realpath(os.path.join(home, ".agent-context"))


def targets_live_store(store=None):
    'Does this run act on the live store? `store` is the tree the script will use when it\n    has its own flag; else AGENT_CONTEXT_STORE; else ~/.agent-context under $HOME.'
    target = store or os.environ.get("AGENT_CONTEXT_STORE") or "~/.agent-context"
    return os.path.realpath(os.path.expanduser(target)) == live_store()


def forward(task, argv, stdin=None, deadline=None):
    'Run `task` with `argv` in the daemon; returns its result dict. Raises\n    store_mcp.StoreUnreachable or store_mcp.ToolError.'
    args = {"task": task, "args": list(argv), "cwd": os.getcwd()}
    if stdin is not None:
        args["stdin"] = stdin
    
    result = store_mcp.call("run_store_task", args, deadline=deadline,
                            read_timeout=deadline or store_mcp.DEADLINE)
    if not isinstance(result, dict) or "exit" not in result:
        raise store_mcp.ToolError("run_store_task answered in an unknown shape")
    return result


def run(task, argv, stdin=None, deadline=None, store=None):
    'What a hook or another script calls: `task` with `argv`, forwarded to the daemon for\n    the live store, run here as a subprocess for a fixture tree. Returns {exit, stdout,\n    stderr}; raises store_mcp.StoreUnreachable or store_mcp.ToolError when the daemon\n    cannot answer.'
    if in_server() or not targets_live_store(store):
        import subprocess
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), task + ".py")
        try:
            proc = subprocess.run([sys.executable, script, *argv], input=stdin or "",
                                  capture_output=True, text=True, timeout=deadline)
        except subprocess.TimeoutExpired:
            raise store_mcp.StoreUnreachable("%s took longer than %ss" % (task, deadline))
        return {"exit": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    return forward(task, argv, stdin, deadline)


def main_or_forward(task, main, stdin=False, deadline=900, store=None):
    "Exit with `main()` inside the daemon or on a fixture tree, else with the forwarded\n    run's exit code. `store`: the tree the script's own flag names, if it has one."
    if in_server() or not targets_live_store(store):
        sys.exit(main())
    data = None
    if stdin and not sys.stdin.isatty():
        data = sys.stdin.read()
    try:
        result = forward(task, sys.argv[1:], data, deadline)
    except (store_mcp.StoreUnreachable, store_mcp.ToolError) as exc:
        sys.stderr.write("%s: the store did not run this task (%s)\n" % (task, exc))
        sys.exit(UNREACHABLE_EXIT)
    sys.stdout.write(result.get("stdout") or "")
    sys.stderr.write(result.get("stderr") or "")
    sys.stdout.flush()
    sys.exit(int(result.get("exit") or 0))
