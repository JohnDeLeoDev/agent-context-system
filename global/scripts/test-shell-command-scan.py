#!/usr/bin/env python3
'Regression battery for shell-command-scan, one case per recorded false positive.\n\nEach case names the observation it comes from. Cases assert on the PARSE,\nwhich is what every guard downstream decides from -- so a guard rewired onto\nthis module inherits all of it.\n\nRun: python3 ~/.agent-context/global/scripts/test-shell-command-scan.py'
import importlib.util
import os
import sys

SCAN = os.path.expanduser("~/.agent-context/global/scripts/shell-command-scan.py")

spec = importlib.util.spec_from_file_location("scs", SCAN)
if spec is None or spec.loader is None:
    raise ImportError("cannot load spec for scs from %s" % SCAN)
scs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scs)

BASE = "/tmp/repo"
fails = []
ran = []


def redirect_texts(cmd):
    r, _ = scs.parse(cmd, BASE)
    return [t.text for _, t in r]


def argv_of(cmd):
    _, segs = scs.parse(cmd, BASE)
    return [[t.text for t in toks] for _, toks, _sep in segs]


def seps_of(cmd):
    _, segs = scs.parse(cmd, BASE)
    return [sep for _, _toks, sep in segs]


def check(name, obs, got, want):
    ran.append(name)
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + name + "   [" + obs + "]")
    if not ok:
        print("         want: %r" % (want,))
        print("         got : %r" % (got,))
        fails.append(name)


print("redirect targets -- the #224 family")
check("fd-dup 2>&1 is not a file named 2", "policy",
      redirect_texts('echo hi > "$S" 2>&1'), ["$S"])
check("stderr redirect to a path is not a file named 2", "policy",
      redirect_texts('cmd 2> "/abs/log.txt"'), ["/abs/log.txt"])
check("gt inside a single-quoted jq filter is not a redirect", "policy",
      redirect_texts("""gh run list --jq 'select(.createdAt > "2026-08-26")'"""), [])
check("2>/dev/null is a redirect, not a grep operand", "policy",
      redirect_texts("grep -rn x /abs/path 2>/dev/null"), ["/dev/null"])
check("arrow inside a trailing comment is not a redirect", "policy",
      redirect_texts("ls /tmp # NO guard here -> the block is skipped"), [])
check("a real redirect IS still caught", "control",
      redirect_texts("cat > src/New.swift"), ["src/New.swift"])
check("heredoc opener keeps its redirect target", "control",
      redirect_texts("cat > src/New.swift <<'EOF'\nbody\nEOF"), ["src/New.swift"])

print("\nheredoc bodies are data -- the #249/#254 family")
check("kotlin >= in a quoted heredoc body", "policy",
      redirect_texts("cat >> .agents/x.md <<'EOF'\nif (a >= b - 500) {}\nEOF"),
      [".agents/x.md"])
check("a () -> T arrow in a heredoc body", "policy",
      redirect_texts("cat >> .agents/x.md <<'EOF'\nfun f(): () -> T\nEOF"),
      [".agents/x.md"])
check("a C# generic in a heredoc body", "policy",
      redirect_texts("cat >> .agents/x.md <<'EOF'\nMap<K, V> m;\nEOF"),
      [".agents/x.md"])
check("python -c writing a store path leaves NO redirect target", "policy",
      redirect_texts("""python3 - <<'PY'\nopen("/x","w")\nPY"""), [])

print("\nsegments and argv -- the #312/#342/#237/#308 family")
check("cat FILE is one segment", "policy",
      argv_of("cat /a/b.txt"), [["cat", "/a/b.txt"]])
check("cat FILE | grep x is TWO segments, both visible", "policy",
      argv_of("cat /a/b.txt | grep x"), [["cat", "/a/b.txt"], ["grep", "x"]])
check("git stash list is readable as argv", "policy",
      argv_of("git stash list"), [["git", "stash", "list"]])
check("git stash push is readable as argv", "policy",
      argv_of("git stash push -m x"), [["git", "stash", "push", "-m", "x"]])

print("separators -- what tells composing from chaining (policy)")
check("a pipe is recorded as a pipe", "policy",
      seps_of("cat /a/b | jq ."), ["|", ""])
check("&& is ONE operator, not two pipes", "policy",
      seps_of("cat /a/b && echo done"), ["&&", ""])
check("|| is ONE operator", "policy",
      seps_of("cmd || fallback"), ["||", ""])
check("a semicolon chain is recorded", "policy",
      seps_of("cat /a/b; echo done"), [";", ""])
check("a background & stays single", "policy",
      seps_of("worker & wait"), ["&", ""])

_, segs = scs.parse("echo 'do not sleep 5 in a loop'", BASE)
quoted = [t.quoted for _, toks, _sep in segs for t in toks]
check("a quoted prose argument is marked quoted=True", "policy",
      quoted, [False, True])

check("a deploy-shaped FILENAME is just an argv word", "policy",
      argv_of("bash /a/verify-deploy-audit-preflight.sh"),
      [["bash", "/a/verify-deploy-audit-preflight.sh"]])

print("\nline continuation -- the production sighting")
check("backslash-newline joins rather than becoming an argument", "production",
      argv_of("ln -sf /a/src \\\n/a/dest"), [["ln", "-sf", "/a/src", "/a/dest"]])

print("\nfail-open shape")
check("empty command yields nothing to judge", "policy/#353",
      scs.parse("", BASE), ([], []))

print()
if fails:
    print("FAILED: %d case(s): %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)




print("all %d cases passed" % len(ran))
