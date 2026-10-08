#!/usr/bin/env python3
'ralph-guard.py: make the ralph-loop Stop hook actually fire for THIS session,\nfrom whatever directory the session is currently in.\n\nPorted from shell in Consolidation Phase 4. Same CLI (none), same file and git effects\nas the shell original. stdout, stderr and exit codes match too, EXCEPT the three\nfailure branches documented below, whose messages this port made reachable (a fixed\nbug, not a preserved one).\n\nThe plugin\'s Stop hook (ralph-loop/1.0.0/hooks/stop-hook.sh) dies silently in two\nindependent ways, and BOTH have to be closed or the loop just stops with no error:\n\n  1. cwd-relative lookup: line 13 reads ".claude/ralph-loop.local.md" relative to the\n     session\'s cwd. Enter a worktree and there is no such file, so the hook exits 0.\n  2. session_id gate: lines 31-35 exit 0 when the state file\'s session_id is not\n     THIS session\'s. A loop launched by an earlier session is dead for every session\n     that inherits the work, which looks identical to a normal turn end.\n\nA symlink does NOT survive: on the first successful fire the hook does\n`sed > TEMP; mv TEMP $RALPH_STATE_FILE`, replacing the symlink with a regular file and\nforking the state. So this installs a real copy and reconciles the forks by mtime.\n\nRun it from the worktree immediately after EnterWorktree, and again any time a turn\nends without the loop re-firing.'

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp







_HOOK_LOOKUP = (r'''set -o pipefail; ls "$1"/.claude/plugins/cache/claude-plugins-official/'''
               r'''ralph-loop/*/hooks/stop-hook.sh 2>/dev/null | tail -1''')
_SESSION_LOOKUP = (r'''set -o pipefail; ls -t "$1"/*.jsonl 2>/dev/null | head -1 '''
                  r'''| xargs -r basename | sed 's/\.jsonl$//' ''')


def main() -> int:
    home = os.environ.get("HOME", "")
    hook_lookup = subprocess.run(["bash", "-c", _HOOK_LOOKUP, "bash", home],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 encoding="utf-8", errors="replace")
    if hook_lookup.returncode != 0:
        
        
        
        print("X ralph-loop plugin not installed.", file=sys.stderr)
        return 1
    hook = (hook_lookup.stdout or "").strip()
    if not hook:
        print("X ralph-loop plugin not installed.", file=sys.stderr)
        return 1

    
    git_proc = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              encoding="utf-8", errors="replace")
    if git_proc.returncode != 0:
        
        
        
        print("X not in a git repository.", file=sys.stderr)
        return 1
    root = re.sub(r"/\.git$", "", (git_proc.stdout or "").rstrip("\n"))
    if not os.path.isdir(root):
        print("X not in a git repository.", file=sys.stderr)
        return 1

    live = None
    live_mtime = None
    for dirpath, _dirnames, filenames in os.walk(os.path.join(root, hp.CLAUDE_DIRNAME)):
        for name in filenames:
            if name != "ralph-loop.local.md":
                continue
            path = os.path.join(dirpath, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            mtime = int(os.stat(path).st_mtime)
            if live_mtime is None or mtime > live_mtime:
                live, live_mtime = path, mtime
    if live is None:
        print("X no ralph-loop.local.md anywhere under %s/.claude — no loop is active."
              % root, file=sys.stderr)
        return 1

    
    key = re.sub(r"[/.]", "-", os.getcwd())
    projdir = os.path.join(hp.projects_dir(), key)
    lookup = subprocess.run(["bash", "-c", _SESSION_LOOKUP, "bash", projdir],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            encoding="utf-8", errors="replace")
    if lookup.returncode != 0:
        
        
        
        
        print("X cannot resolve this session's id under %s" % projdir, file=sys.stderr)
        return 1
    session = (lookup.stdout or "").strip()
    if not session:
        print("X cannot resolve this session's id under %s" % projdir, file=sys.stderr)
        return 1

    
    os.makedirs(hp.CLAUDE_DIRNAME, exist_ok=True)
    target = os.path.join(hp.project_claude_dir(os.getcwd()), "ralph-loop.local.md")
    same = os.path.exists(target) and os.path.samefile(live, target)
    if not same:
        shutil.copy(live, target)

    with open(target, encoding="utf-8") as fh:
        head, sep, body = fh.read().partition("\n---\n")
    if not sep:
        print("frontmatter delimiter not found in " + target, file=sys.stderr)
        return 1
    if re.search(r"^session_id:", head, re.M):
        head = re.sub(r"^session_id: .*$", "session_id: " + session, head, flags=re.M)
    else:
        head = head.rstrip("\n") + "\nsession_id: " + session
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(head + sep + body)

    
    
    
    sandbox = tempfile.mkdtemp()
    try:
        os.makedirs(hp.project_claude_dir(sandbox), exist_ok=True)
        shutil.copy(target, os.path.join(hp.project_claude_dir(sandbox), "ralph-loop.local.md"))
        transcript = os.path.join(projdir, session + ".jsonl")
        payload = '{"session_id":"%s","transcript_path":"%s"}' % (session, transcript)
        try:
            hook_proc = subprocess.run(["bash", hook], input=payload, cwd=sandbox,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       encoding="utf-8", errors="replace")
            text = (hook_proc.stdout or "").strip()
            verdict = json.loads(text).get("decision", "") if text else ""
        except Exception:
            verdict = ""

        if verdict != "block":
            print("X ralph guard NOT armed — the hook would let this session stop silently.",
                  file=sys.stderr)
            print("   state:   " + target, file=sys.stderr)
            print("   session: " + session, file=sys.stderr)
            print("   Check the promise tag in the last message, and max_iterations vs iteration.",
                  file=sys.stderr)
            return 1

        print("ralph guard armed: hook returns decision=block")
        print("  state:   " + target)
        print("  session: " + session)
        return 0
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
