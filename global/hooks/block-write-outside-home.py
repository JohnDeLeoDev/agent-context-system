#!/usr/bin/env python3
'PreToolUse(Bash|Write|Edit|MultiEdit|NotebookEdit|Monitor): refuse writes outside $HOME.\n\nAPPROVAL is a grant user gives by picking Approve on an approval question, which\nthe approval-question hook turns into a write-outside-home-consent.py grant:\n  Allow agent writes under <dir> for the next <minutes> minutes? [approval:write-outside-home:<dir>:<minutes>]\nA permission prompt cannot do this job: in bypassPermissions an `ask` rule\nresolves to a silent deny (memory claude-code-permission-modes-and-the-ask-trap).\nblock-consent-self-grant refuses an agent creating the grant itself.\n\nONE SCRIPT FOR EVERY HARNESS. It blocks with stderr plus exit 2, which every\nharness honors, and never exits nonzero for any other reason, because the\nopencode plugin treats every nonzero exit as a block.\n\nWHAT IT CANNOT SEE: writes a program makes on its own, such as a compiler\nusing $TMPDIR, or the body of a script file. Build tools keep working because\nof that gap. THE SAME GAP applies to a target this module truly cannot\nresolve ahead of time -- `$(...)` command substitution, a value read from a\nfile, a variable this command never assigns -- which stays allowed rather\nthan guessed at; a bare, unassigned `$VAR` with no absolute or `~` prefix in\nfront of it is the same case (`> "$f"` from `$(...)` is the canonical\nexample). This is a real, documented gap: an agent that wanted to write\noutside home and knew this rule could still route the target through an\nunassigned variable or a subshell. It is not closed here because closing it\nwould mean predicting the output of an arbitrary subshell ahead of running\nit, which is undecidable in general -- the honest fix is fewer places able to\noriginate an unresolvable target in the first place, not a guess in this\nhook.\n\nFails OPEN on an unparseable payload or a missing parser, with a stderr note, so\na broken projection cannot brick every tool call.'
import importlib.util
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

HOME = os.path.realpath(os.path.expanduser("~"))
STATE = os.path.join(os.environ.get("XDG_STATE_HOME")
                     or os.path.join(os.path.expanduser("~"), ".local", "state"),
                     "agent-context")
GRANTS = os.path.join(STATE, "write-outside-home-consent")
LOG = GRANTS + ".log"
DEV_OK = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/zero")
PATH_KEYS = ("file_path", "filePath", "path", "notebook_path", "notebookPath",
             "target_file", "targetFile")
SCAN = os.environ.get("SHELL_COMMAND_SCAN") or os.path.join(
    hp.scripts_dir(), "shell-command-scan.py")

TEMP_ROOT = re.compile(r"(?<![\w.~$-])(/(?:private|tmp|var/folders|var/tmp)(?:/[^\s'\"),;]*)?)")
INTERP = re.compile(r"\b(python3?|perl|ruby|node)\b")
WRITE = re.compile(r"\.write\(|\.write_text\(|\.write_bytes\(|writeFileSync|"
                   r"makedirs\(|mkdir\(|open\([^)]*['\"][wax]b?\+?['\"]")







PY_OPEN = re.compile(r"""open\(\s*['"]([^'"]+)['"]\s*,\s*['"][wax]b?\+?['"]""")
PY_PATH_WRITE = re.compile(r"""Path\(\s*['"]([^'"]+)['"]\s*\)\.write_text\(""")



FAST_PATH = re.compile(
    r'"run_in_background": *true|"tool_name": *"Monitor"|>|of=|file_?[Pp]ath|notebook_?[Pp]ath|'
    r'"path"|target_?[Ff]ile|(^|[^A-Za-z0-9_.-])(tee|sed|perl|ruby|gsed|python3?|node|cp|mv|install|'
    r'rsync|ln|truncate|shred|touch|mkdir|mktemp|rm|rmdir|curl|wget|tar|unzip|git)([^A-Za-z0-9_.-]|$)',
    re.MULTILINE)


def note(msg):
    sys.stderr.write("block-write-outside-home: " + msg + "\n")


def resolve(base, t):
    t = (t or "").strip()
    if not t or "$" in t:
        return None
    t = os.path.expanduser(t)
    if not os.path.isabs(t):
        t = os.path.join(base, t)
    return os.path.normpath(t)


def outside(p):
    if p.startswith("/dev/"):
        return not (p in DEV_OK or p.startswith("/dev/fd/"))
    r = os.path.realpath(p)
    return not (r == HOME or r.startswith(HOME + os.sep))


def granted(p):
    r = os.path.realpath(p)
    try:
        with open(GRANTS) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return False
    now = time.time()
    for ln in lines:
        parts = ln.split("\t")
        if len(parts) < 2 or not parts[1] or parts[1] == "/":
            continue
        try:
            if float(parts[0]) <= now:
                continue
        except ValueError:
            continue
        pre = parts[1].rstrip("/")
        if r == pre or r.startswith(pre + "/"):
            return True
    return False


def creator_targets(scs, cmd, cwd):
    'Writers shell-command-scan.write_targets does not list.'
    out = []
    _, segments = scs.parse(cmd, cwd)
    env = scs.collect_env(segments)

    def resolve_all(d, t):
        "Every candidate a creator's target `t` resolves to, `$`-aware via\n        shell-command-scan.resolve_candidates. See that module for what a\n        target holding an unresolvable variable falls back to."
        t = (t or "").strip()
        if not t:
            return []
        if "$" not in t:
            p = resolve(d, t)
            return [p] if p else []
        return scs.resolve_candidates(d, t, env)

    for seg in segments:
        d, argv = seg[0], [t.text for t in seg[1]]
        k = 0
        while k < len(argv) and "=" in argv[k] and not argv[k].startswith("-"):
            k += 1
        for pre in ("sudo", "command", "nohup", "env", "time", "xargs"):
            if k < len(argv) and os.path.basename(argv[k]) == pre:
                k += 1
        if k >= len(argv):
            continue
        name, rest = os.path.basename(argv[k]), argv[k + 1:]
        words = [w for w in rest if w and not w.startswith("-")]
        cands = []
        if name in ("touch", "mkdir", "rm", "rmdir"):
            cands = words
        elif name == "mktemp":
            
            
            
            
            
            pdir = None
            for i, w in enumerate(rest):
                if w in ("-p", "--tmpdir") and i + 1 < len(rest):
                    pdir = rest[i + 1]
                elif w.startswith("--tmpdir="):
                    pdir = w.split("=", 1)[1]
                elif w.startswith("-p") and len(w) > 2:
                    pdir = w[2:]
            templates = [w for w in words if w != pdir]
            if pdir:
                cands = [os.path.join(pdir, "mktemp")]
            elif any("/" in t for t in templates):
                cands = [t for t in templates if "/" in t]
            elif sys.platform == "darwin":
                cands = ["/private/var/folders/mktemp"]
            else:
                cands = [os.path.join(os.environ.get("TMPDIR") or "/tmp", "mktemp")]
        else:
            flags = {"curl": ("-o", "--output"), "wget": ("-O", "--output-document"),
                     "tar": ("-C", "--directory"), "unzip": ("-d",)}.get(name, ())
            for i, w in enumerate(rest):
                if w in flags and i + 1 < len(rest):
                    cands.append(rest[i + 1])
                for f in flags:
                    if f.startswith("--") and w.startswith(f + "="):
                        cands.append(w[len(f) + 1:])
            if name == "git" and words[:1] == ["clone"] and len(words) >= 3:
                cands.append(words[-1])
        out += [p for c in cands for p in resolve_all(d, c)]
    return out


def main():
    raw = sys.stdin.read()
    if not FAST_PATH.search(raw):
        return 0

    try:
        d = json.loads(raw)
    except Exception:
        return 0
    if not isinstance(d, dict):
        return 0
    ti = d.get("tool_input") or d.get("tool_args") or d.get("params") or {}
    if not isinstance(ti, dict):
        ti = {}
    tool = d.get("tool_name") or ""
    cwd = d.get("cwd") or os.getcwd()
    
    
    
    
    
    
    task_root = os.environ.get("CLAUDE_CODE_TMPDIR") or ""
    if not os.path.isabs(task_root):
        task_root = "/private/tmp"
    task_root = os.path.realpath(task_root)
    if (tool == "Monitor" or ti.get("run_in_background") is True) and outside(task_root):
        sys.stderr.write(
            "BLOCKED by block-write-outside-home: a background task (%s) makes Claude Code\n"
            "write its output under %s, outside the home directory.\n\n"
            "CLAUDE_CODE_TMPDIR in settings.json moves it inside home; a session started\n"
            "before that setting has to be restarted to pick it up. Until then run it in the\n"
            "foreground. To wait on something (a sync, a pull, a build), use ONE bounded\n"
            "foreground command that re-checks the condition and does the step once it\n"
            "holds, with a timeout under the Bash tool's limit.\n"
            % ("Monitor" if tool == "Monitor" else "Bash run_in_background", task_root))
        return 2
    cmd = ti.get("command") or ti.get("cmd") or ""
    targets, interp = [], []

    if isinstance(cmd, str) and cmd:
        try:
            spec = importlib.util.spec_from_file_location("scs", SCAN)
            if spec is None or spec.loader is None:
                raise ImportError("cannot load " + SCAN)
            scs = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(scs)
            targets += scs.write_targets(cmd, cwd)
            targets += creator_targets(scs, cmd, cwd)
        except Exception as e:
            note("FAILING OPEN, cannot parse with %s (%s). This guard is DISABLED "
                 "for this call." % (SCAN, e))
            return 0
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        deq = scs.unescape_dquotes(cmd)
        if INTERP.search(cmd) and (WRITE.search(cmd) or WRITE.search(deq)):
            interp = [m.group(1) for m in TEMP_ROOT.finditer(cmd)]
            for rx in (PY_OPEN, PY_PATH_WRITE):
                for m in rx.finditer(deq):
                    lit = m.group(1)
                    if lit.startswith("/") or lit.startswith("~"):
                        p = resolve(cwd, lit)
                        if p and outside(p):
                            interp.append(p)
    else:
        p = next((ti[k] for k in PATH_KEYS if isinstance(ti.get(k), str) and ti.get(k)), "")
        p = resolve(cwd, p)
        if not p:
            return 0
        targets.append(p)

    hits = sorted({p for p in targets if outside(p)} | set(interp))
    if not hits:
        return 0
    left = [p for p in hits if not granted(p)]
    if not left:
        try:
            with open(LOG, "a") as fh:
                for p in hits:
                    fh.write("%s\tALLOW\t%s\t%s %s\n" % (
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), p,
                        d.get("session_id") or "-", tool or "-"))
        except OSError:
            pass
        return 0

    msg = ["BLOCKED by block-write-outside-home: this writes outside the home "
           "directory (%s):" % HOME]
    msg += ["  " + p for p in left]
    if interp:
        msg += ["", "An interpreter script names a temp path and writes. If it only "
                "READS that path, split the read into its own command."]
    msg += [
        "",
        "Put scratch files inside home:",
        "  the harness scratchpad                   files for this turn, when CLAUDE_CODE_TMPDIR is inside home",
        "  <project>/.agents/tmp/                   files for this turn otherwise (check .gitignore covers it)",
        "  ~/.local/state/agent-scratch/<session>/  state that must survive to the next turn",
        "Never ~/.agents/tmp.",
        "",
        "If this path is really required, ask user with one AskUserQuestion: header",
        "\"Approval\", options \"Approve\" (its description says why) and \"Deny\", question:",
        "    Allow agent writes under <resolved dir outside home> for the next <1-240> minutes? [approval:write-outside-home:<resolved dir outside home>:<minutes>]",
        "The approval-question hook creates the grant when he picks Approve. Never create it yourself.",
    ]
    sys.stderr.write("\n".join(msg) + "\n")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  
        note("FAILING OPEN on an internal error: %s" % e)
        sys.exit(0)
