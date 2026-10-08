#!/usr/bin/env python3
'Battery for block-write-outside-home. Run against the PROJECTION:\n  python3 ~/.agent-context/global/scripts/hook-test-run.py --hook block-write-outside-home\nor directly:\n  python3 ~/.agent-context/global/scripts/test-block-write-outside-home.py\nThe store copy is non-executable by design; test the materialized hook.'
import json
import os
import subprocess
import sys
import tempfile
import time

HOOK = os.environ.get("HOOK") or os.path.expanduser("~/.agent-context/global/hooks/block-write-outside-home.py")
HOME = os.path.expanduser("~")

os.makedirs(os.path.join(HOME, ".local", "state"), exist_ok=True)
XDG_STATE_HOME = tempfile.mkdtemp(dir=os.path.join(HOME, ".local", "state"), prefix="bwoh-test.")
os.makedirs(os.path.join(XDG_STATE_HOME, "agent-context"), exist_ok=True)
GRANTS = os.path.join(XDG_STATE_HOME, "agent-context", "write-outside-home-consent")

pass_n = 0
fail_n = 0


def cleanup():
    for p in (GRANTS, GRANTS + ".log"):
        try:
            os.remove(p)
        except OSError:
            pass
    for d in (os.path.join(XDG_STATE_HOME, "agent-context"), XDG_STATE_HOME):
        try:
            os.rmdir(d)
        except OSError:
            pass


def bash_payload(command, cwd=None):
    return json.dumps({"tool_name": "Bash", "cwd": cwd or HOME, "tool_input": {"command": command}})


def path_payload(path, cwd=None, tool="Write", key="file_path", wrapper="tool_input"):
    return json.dumps({"tool_name": tool, "cwd": cwd or HOME, wrapper: {key: path}})


def bg_payload(tool, command, background):
    return json.dumps({"tool_name": tool, "cwd": HOME,
                        "tool_input": {"command": command, "description": "d",
                                       "run_in_background": background == "1"}})


def expect(want, label, payload, env_overrides=None):
    global pass_n, fail_n
    env = {**os.environ, "XDG_STATE_HOME": XDG_STATE_HOME}
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.run([sys.executable, HOOK], input=payload, capture_output=True,
                           text=True, env=env)
    got = proc.returncode
    ok = (want == "block" and got == 2) or (want == "allow" and got == 0)
    if ok:
        pass_n += 1
    else:
        fail_n += 1
        print("FAIL (want %s, exit %d): %s" % (want, got, label))
    return proc


try:
    
    expect("block", "Write /tmp", path_payload("/tmp/bwoh-x"))
    expect("block", "Write harness scratchpad", path_payload("/private/tmp/claude-501/s/x"))
    expect("block", "opencode filePath /var", path_payload("/var/folders/zz/x", HOME, "write", "filePath"))
    expect("block", "pi path relative, cwd /tmp", path_payload("x.txt", "/tmp", "write", "path", "tool_args"))
    expect("block", "NotebookEdit /opt", path_payload("/opt/n.ipynb", HOME, "NotebookEdit", "notebook_path"))
    expect("block", "other user", path_payload("/Users/Shared/x"))
    expect("allow", "Write inside home", path_payload(os.path.join(HOME, ".local/state/agent-scratch/s/x")))
    expect("allow", "relative inside home", path_payload("notes.txt", HOME))

    
    expect("block", "redirect /tmp", bash_payload("echo hi > /tmp/bwoh-x"))
    expect("block", "append /private/tmp", bash_payload("echo hi >> /private/tmp/bwoh-x"))
    expect("block", "tee", bash_payload("echo hi | tee /tmp/bwoh-x"))
    expect("block", "cp into /tmp", bash_payload("cp a.txt /tmp/"))
    expect("block", "cd /tmp then relative", bash_payload("cd /tmp && echo x > y"))
    expect("block", "cwd /tmp relative redirect", bash_payload("echo x > y", "/tmp"))
    expect("block", "touch", bash_payload("touch /tmp/bwoh-x"))
    expect("block", "mkdir -p", bash_payload("mkdir -p /tmp/bwoh-dir/sub"))
    expect("block", "rm", bash_payload("rm /tmp/bwoh-x"))
    expect("block", "curl -o", bash_payload("curl -sL -o /tmp/f https://example.com"))
    expect("block", "git clone dest", bash_payload("git clone https://example.com/r.git /tmp/r"))
    expect("block", "tar -C", bash_payload("tar -xzf a.tgz -C /tmp"))
    expect("block", "python open w", bash_payload("python3 -c \"open('/tmp/bwoh-x','w').write('1')\""))
    expect("block", "python heredoc write",
           bash_payload('python3 - <<PY\np = "/private/tmp/x"\nopen(p, "w").write("1")\nPY'))

    
    expect("allow", "redirect /dev/null", bash_payload("ls > /dev/null 2>&1"))
    expect("allow", "ls /tmp", bash_payload("ls -la /tmp"))
    expect("allow", "grep pipe", bash_payload("grep -rn foo /tmp | wc -l"))
    expect("allow", "python read only", bash_payload("python3 -c \"print(open('/tmp/x').read())\""))
    expect("allow", "heredoc body mentions /tmp",
           bash_payload("cat > %s/bwoh-notes.txt <<EOF\necho x > /tmp/y\nEOF" % HOME))
    expect("allow", "redirect inside home", bash_payload("echo x > %s/bwoh-x" % HOME))
    expect("allow", "git status", bash_payload("git status"))
    expect("allow", "unparseable payload", "not json")

    
    
    
    
    expect("block", "run_in_background, CLAUDE_CODE_TMPDIR unset",
           bg_payload("Bash", "make test", "1"), {"CLAUDE_CODE_TMPDIR": ""})
    expect("block", "Monitor, CLAUDE_CODE_TMPDIR unset",
           bg_payload("Monitor", "tail -f build.log", "0"), {"CLAUDE_CODE_TMPDIR": ""})
    expect("block", "run_in_background, CLAUDE_CODE_TMPDIR outside home",
           bg_payload("Bash", "make test", "1"), {"CLAUDE_CODE_TMPDIR": "/private/tmp"})
    expect("block", "run_in_background, relative CLAUDE_CODE_TMPDIR",
           bg_payload("Bash", "make test", "1"), {"CLAUDE_CODE_TMPDIR": "claude-tmp"})
    expect("allow", "run_in_background, CLAUDE_CODE_TMPDIR in home",
           bg_payload("Bash", "make test", "1"), {"CLAUDE_CODE_TMPDIR": HOME + "/.cache/claude-tmp"})
    expect("allow", "Monitor, CLAUDE_CODE_TMPDIR in home",
           bg_payload("Monitor", "tail -f build.log", "0"), {"CLAUDE_CODE_TMPDIR": HOME + "/.cache/claude-tmp"})

    
    link = os.path.join(XDG_STATE_HOME, "bwoh-tmplink")
    os.symlink("/private/tmp", link)
    proc = subprocess.run(
        [sys.executable, HOOK], input=bg_payload("Bash", "make test", "1"),
        capture_output=True, text=True,
        env={**os.environ, "XDG_STATE_HOME": XDG_STATE_HOME, "CLAUDE_CODE_TMPDIR": link})
    os.remove(link)
    if "output under /private/tmp," in proc.stderr:
        pass_n += 1
    else:
        fail_n += 1
        print("FAIL (want the resolved /private/tmp in the refusal): symlinked CLAUDE_CODE_TMPDIR")

    expect("allow", "Bash foreground", bg_payload("Bash", "git status", "0"))

    
    
    
    expect("block", "bare mktemp -d", bash_payload("mktemp -d"), {"TMPDIR": "/tmp"})
    expect("block", "mktemp -p /tmp", bash_payload("mktemp -p /tmp x.XXXX"))
    expect("block", "mktemp template in /tmp", bash_payload("mktemp /tmp/bwoh.XXXXXX"))
    expect("allow", "mktemp -p inside home", bash_payload("mktemp -d -p %s/.cache/tmp" % HOME))
    expect("allow", "mktemp template inside home", bash_payload("mktemp %s/.cache/tmp/bwoh.XXXXXX" % HOME))

    
    now = int(time.time())
    with open(GRANTS, "w") as fh:
        fh.write("%d\t%s\ttest\n" % (now + 600, os.path.realpath("/tmp/bwoh-granted")))
    expect("allow", "granted dir, Write", path_payload("/tmp/bwoh-granted/a/b"))
    expect("allow", "granted dir, redirect", bash_payload("echo x > /tmp/bwoh-granted/f"))
    expect("block", "sibling of granted dir", path_payload("/tmp/bwoh-granted-not/x"))
    expect("block", "other dir while granted", path_payload("/tmp/other"))
    with open(GRANTS, "w") as fh:
        fh.write("%d\t/private/tmp/bwoh-granted\ttest\n" % (now - 5))
    expect("block", "expired grant", path_payload("/tmp/bwoh-granted/a"))
    with open(GRANTS, "w") as fh:
        fh.write("%d\t/\ttest\n" % (now + 600))
    expect("block", "grant for / is ignored", path_payload("/tmp/x"))
finally:
    cleanup()

print("block-write-outside-home: %d passed, %d failed" % (pass_n, fail_n))
sys.exit(0 if fail_n == 0 else 1)
